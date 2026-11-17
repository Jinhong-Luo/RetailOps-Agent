"""固定业务轨迹的已授权操作和只读请求，比较现行服务与早期按钮服务的故障恢复。"""
import asyncio
from copy import deepcopy
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from ecom_paired import Agent, Issue, MockStoryEnv, ROOT, RunLogger, Shop, Store, dump, enable_refund, server
from retailops.domain import Domain
from shared_fixture import ORDER_IDS, ecom_case, tau_case
import httpx


EARLY = "021732d"


def archived_classes(folder):
    classes=[]
    for file, cls in [("store","Store"),("agent","Agent")]:
        source=subprocess.check_output(["git","-C",ROOT,"show",f"{EARLY}:retailops/{file}.py"])
        path=folder/(file+".py")
        path.write_bytes(source)
        spec=importlib.util.spec_from_file_location("retailops.回放旧"+file,path)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        classes.append(getattr(module,cls))
    return classes


def account(store, sid):
    with store.connection() as db:
        uid=store.row(db,sid)["uid"]
        return json.loads(db.execute("SELECT body FROM accounts WHERE uid=?",(uid,)).fetchone()[0])


def upstream_calls(trace):
    rows=[]
    for item in trace:
        f=item["function"]
        args=f["arguments"]
        rows.append((f["name"],json.loads(args) if isinstance(args,str) else args))
    return rows


def from_sql(database):
    with sqlite3.connect("file:"+str(database)+"?mode=ro",uri=True) as db:
        receipts=[]
        for uid,blob in db.execute("SELECT s.uid,r.body FROM receipts r JOIN sessions s USING(sid) ORDER BY r.rowid"):
            receipts.append((uid,json.loads(blob)))
        history=[m for (blob,) in db.execute("SELECT history FROM sessions ORDER BY rowid") for m in json.loads(blob)]
        final={uid:json.loads(blob) for uid,blob in db.execute("SELECT uid,body FROM accounts")}
    return receipts, history, final


def tau_source(folder):
    data=json.loads((folder/"native/zh-retail/results.json").read_bytes())
    sim=data["simulations"][0]
    domain=Domain()
    messages=sim["messages"]
    tools={m["id"]:m for m in messages if m["role"]=="tool"}
    uid=None
    reads=[]
    groups=[]
    group=[]
    current_version=0
    for m in messages:
        calls=m.get("tool_calls") or []
        for c in calls:
            name,args=c["name"],c["arguments"]
            result=tools.get(c["id"],{})
            if result.get("error") or "content" not in result:
                continue
            if name.startswith("find_user_id_by_") and result["content"] in domain.seed["users"]:
                uid=result["content"]
            elif name=="click_confirm":
                if group:
                    groups.append((uid,dict(version=current_version,items=group)))
                    group=[]
                current_version=args["version"]
            elif name in domain.reads and uid:
                reads.append((name,args))
            elif name in domain.writes:
                group.append(dict(tool=name,args=args,result=json.loads(result["content"])))
    if group:
        groups.append((uid,dict(version=current_version,items=group)))
    text=next((m["content"] for m in messages if m["role"]=="user" and m.get("content")),"")
    return domain, [uid] if uid else [],groups,reads,text,{}


def source(row, folder):
    if row.startswith("tau-"):
        return tau_source(folder)
    shared=not row.startswith("ecom-")
    if not shared:
        env=MockStoryEnv(task_index=int(row.split("-")[1]),console_verbose=RunLogger(False))
        domain=Shop(env)
        enable_refund(domain)
        domain.scoped_waits=True
        database=folder/"business.db"
    else:
        domain=ecom_case(row,"full") if row in ORDER_IDS else tau_case(row)[0]
        database=folder/"账本.db"
    receipts,history,final=from_sql(database)
    reads=[]
    for message in history:
        for name,args in upstream_calls(message.get("tool_calls") or []):
            if name in domain.reads and (name not in domain.writes or args.get("action") in {"查询","余额查询","信息查询"}):
                reads.append((name,args))
    text=next((m["content"] for m in history if m["role"]=="user" and m.get("content")),"")
    return domain,list(final),receipts,reads,text,final


def credential(domain,uid):
    return uid if isinstance(domain,Shop) else domain.seed["users"][uid]["email"]


def receipts_count(store):
    with store.connection() as db:
        return db.execute("SELECT count(*) FROM receipts").fetchone()[0]


async def read_trial(store,agent_class,sid,uid,reads,text,fault):
    chosen=None
    for name,args in reads:
        try:
            expected=store.read(sid,name,args)
        except (Issue,ValueError,TypeError,KeyError):
            continue
        chosen=(name,args,expected)
        break
    if not chosen or not text:
        return dict(状态="未触发",原因="没有足以重建已核实用户只读请求的证据")
    name,args,expected=chosen
    before=account(store,sid)
    observed=[]
    inject=fault

    async def recorded(request):
        nonlocal inject
        body=json.loads(request.content)
        if inject:
            inject=False
            raise httpx.ReadTimeout("测试注入的传输超时",request=request)
        if body["messages"][-1].get("role")!="tool":
            message=dict(role="assistant",content=None,tool_calls=[dict(id="读",type="function",
                         function=dict(name=name,arguments=dump(args)))])
        else:
            observed.append(json.loads(body["messages"][-1]["content"])==expected)
            message=dict(role="assistant",content="该次只读查询已取得回执。")
        return httpx.Response(200,json={"choices":[{"message":message}]})
    async with httpx.AsyncClient(base_url="http://test/",transport=httpx.MockTransport(recorded)) as client:
        # 回放从指定原始读请求开始，仅对服务生命周期施加故障，保持两臂的录制模型返回相同。
        agent=agent_class(store,client)
        original=getattr(store.domain,"opening_read",None)
        product=getattr(store.domain,"product_consultation",False)
        if original:
            store.domain.opening_read=None
            store.domain.product_consultation=False
        rollback=True
        try:
            if fault:
                try:
                    await agent.run(sid,text,store.snapshot(sid)[0]["revision"],"B","首次只读")
                    rollback=False
                except Issue as e:
                    rollback=e.data["code"]=="timeout" and before==account(store,sid) and not agent.active
            await agent.run(sid,text,store.snapshot(sid)[0]["revision"],"B","重试只读" if fault else "首次只读")
        finally:
            if original:
                store.domain.opening_read=original
                store.domain.product_consultation=product
    return dict(状态="已回放",读工具=name,查询值相符=observed==[True],注入超时=fault,
                席位释放=not agent.active,账本不变=before==account(store,sid),失败时回滚=rollback)


def exercise(row,source_folder,dirpath,fault,old):
    dirpath.mkdir(parents=True,exist_ok=False)
    store_class,agent_class=archived_classes(dirpath) if old else (Store,Agent)
    domain,users,groups,reads,text,final=source(row,source_folder)
    if not users:
        return dict(状态="未触发",原因="轨迹未完成顾客身份核验")
    if any(uid not in users for uid,_ in groups):
        return dict(状态="未验证",原因="已执行操作缺乏已核身份对应关系")
    path=dirpath/"回放.db"
    store=store_class(path,domain)
    sessions={uid:store.identifier(store.open(credential(domain,uid))) for uid in users}
    if not groups:
        sid=sessions[users[0]]
        result=asyncio.run(read_trial(store,agent_class,sid,users[0],reads,text,fault))
        if result["状态"]=="已回放":
            result["通过"]=all(result[k] for k in ["查询值相符","席位释放","账本不变","失败时回滚"])
        return result
    results=[]
    for num,(uid,original) in enumerate(groups):
        sid=sessions[uid]
        actions=[dict(tool=a["tool"],args=a["args"]) for a in original["items"]]
        state=store.propose(sid,store.snapshot(sid)[0]["revision"],actions)
        v=state["version"]
        store.confirm(sid,v)
        before=account(store,sid)
        count=receipts_count(store)
        request=original.get("request_id") or f"提交{num}"
        rollback=True
        if fault:
            with store.connection(True) as db:
                db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'注入回执失败'); END")
            try:
                store.execute(sid,v,request)
                rollback=False
            except sqlite3.IntegrityError:
                rollback=account(store,sid)==before and receipts_count(store)==count
            with store.connection(True) as db:
                db.execute("DROP TRIGGER fail_receipt")
        receipt=store.execute(sid,v,request)
        if fault:
            store=store_class(path,domain)
            retry=store.execute(sid,v,request)
            once=retry["replayed"] and retry["results"]==receipt["results"]
        else:
            once=True
        results.append(dict(工具=[a["tool"] for a in actions],原回执逐项相符=receipt["results"]==[i["result"] for i in original["items"]],
                            半执行回滚=rollback,重启重试幂等=once,仅一份回执=receipts_count(store)==count+1))
    same=all(account(store,sessions[uid])==ref for uid,ref in final.items()) if final else None
    return dict(状态="已回放",组数=len(results),原终态相同=same,逐组=results,
                通过=same is not False and all(all(g[k] for k in ("原回执逐项相符","半执行回滚","重启重试幂等","仅一份回执")) for g in results))


def replay(row,src,folder,fault):
    server.set_data=lambda data:None
    out={}
    for name,old in (("冻结服务",False),("早期按钮服务",True)):
        try:
            out[name]=exercise(row,src,folder/name,fault,old)
        except (Issue,ValueError,KeyError,TypeError,RuntimeError) as e:
            out[name]=dict(状态="未验证",原因=e.data["code"] if isinstance(e,Issue) else type(e).__name__)
            (folder/name/"复核异常.txt").write_text(str(e),encoding="utf8")
    return out
