"""按共享排期在新副本中执行 D17—D24 的授权、服务和评分回放。"""
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
import shutil
from types import SimpleNamespace

import httpx
from ecom_development import fixture
from ecom_paired import ButtonService, Shop, MockStoryEnv, RunLogger, Store, Issue, enable_refund, server
from retailops.api import create_app
from retailops.domain import Domain


def ledger(store,uid):
    with store.connection() as db:
        return json.loads(db.execute("SELECT body FROM accounts WHERE uid=?",(uid,)).fetchone()[0])


def receipt_count(store):
    with store.connection() as db:
        return db.execute("SELECT count(*) FROM receipts").fetchone()[0]


def prepare(store,sid,actions):
    version=store.propose(sid,store.snapshot(sid)[0]["revision"],actions)["version"]
    store.confirm(sid,version)
    return version


def version_replay(folder,alter):
    shop=fixture()
    initial=shop.initial(shop.uid)
    initial["users_info"][shop.uid]["电话号码"]="13000000003"
    shop.initial=lambda uid:initial
    store=Store(folder/"测试.db",shop)
    sid=store.identifier(store.open(shop.uid))
    urgent=dict(tool="manage_urgent_tool",args=dict(platform="jd",shop_id="开发店",user_id=shop.uid,order_id="71001"))
    invoice=dict(tool="manage_invoice_tool",args=dict(order_id="71001",title="青禾",phone_number="13000000003",invoice_type="个人发票"))
    before=ledger(store,shop.uid)
    first=store.propose(sid,0,[urgent,invoice])
    state,_=store.begin_turn(sid,"票先别开，只催这笔甲单",first["revision"],"修订")
    second=store.propose(sid,state["revision"],[urgent])
    service=SimpleNamespace(store=store,sid=sid,version=second["version"])
    try:
        # 缺陷仅注入隔离的回放调用，将旧按钮错误映射为现版。
        ButtonService.click(service,second["version"] if alter else first["version"])
        accepted=True
    except ValueError:
        accepted=False
    unchanged=before==ledger(store,shop.uid) and receipt_count(store)==0
    ButtonService.click(service,second["version"])
    res=store.execute(sid,second["version"],"合法确认")
    correct=[item["tool"] for item in res["items"]]==["manage_urgent_tool"] and receipt_count(store)==1
    return dict(迟到确认被接受=accepted,确认时账本未变=unchanged,合法提交仅加急=correct,
                授权业务检查通过=not accepted and unchanged and correct,检验符合预期=accepted==alter and unchanged and correct)


def grouped_refund(folder,protect):
    outcomes=[]
    for complete in [True,False]:
        shop=fixture()
        if protect:
            enable_refund(shop)
        store=Store(folder/str(complete)/"测试.db",shop)
        sid=store.identifier(store.open(shop.uid))
        args=dict(platform="jd",user_id=shop.uid,shop_id="开发店",order_id="71001")
        actions=[dict(tool="manage_order_tool",args=dict(args,action="取消"))]
        if complete:
            actions.append(dict(tool="manage_ecard_tool",args=dict(args,action="退款",amount=168,refund_reason="取消")))
        try:
            version=prepare(store,sid,actions)
            store.execute(sid,version,"确认")
        except Issue:
            pass
        data=ledger(store,shop.uid)
        outcomes.append(dict(完整=complete,状态=data["orders"]["jd"]["开发店"][shop.uid]["71001"]["订单状态"],
                             余额=data["users_info"][shop.uid]["电子卡余额"],回执=receipt_count(store)))
    valid,missing=outcomes
    safe=missing["状态"]=="已付款" and missing["余额"]==32 and missing["回执"]==0
    correct=valid["状态"]=="已取消" and valid["余额"]==200 and valid["回执"]==1
    return dict(回放=outcomes,业务通过=correct and safe,检验符合预期=correct and safe==protect)


async def session_replay(folder,cancel):
    entered,release=asyncio.Event(),asyncio.Event()
    async def recorded(request):
        entered.set()
        await release.wait()
        return httpx.Response(200,json={"choices":[{"message":{"role":"assistant","content":"已核对本窗口待办。"}}]})
    app=create_app(folder/"服务.db",httpx.MockTransport(recorded),capacity=1,waiting=1,timeout=15)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        store=app.state.store
        user=next(iter(store.domain.seed["users"].values()))
        headers=[]
        for _ in range(2):
            token=(await client.post("/sessions",json={"email":user["email"]})).json()["token"]
            headers.append({"Authorization":"Bearer "+token})
        before=ledger(store,user["user_id"])
        def request(index):
            return asyncio.create_task(client.post("/messages",headers=headers[index],json=dict(message="请重新核对我的待办",
                                       request_id="甲",revision=0)))
        first=request(0)
        await asyncio.wait_for(entered.wait(),3)
        second=request(1)
        for _ in range(300):
            if len(app.state.agent.active)==2:
                break
            await asyncio.sleep(.01)
        assert len(app.state.agent.active)==2
        if cancel:
            (await client.delete("/messages/甲",headers=headers[0])).raise_for_status()
        release.set()
        a,b=await asyncio.gather(first,second)
        ok=(a.json().get("code")=="cancelled" if cancel else a.is_success) and b.is_success
        isolated=not app.state.agent.active and before==ledger(store,user["user_id"])
        return dict(甲状态=a.status_code,乙状态=b.status_code,席位释放及账本未变=isolated,检验符合预期=ok and isolated)


def tau_replay(folder,case,fault):
    domain=Domain()
    path=folder/"账本.db"
    store=Store(path,domain)
    oid="#W4817420" if case=="D20" else "#W6779827"
    uid=domain.seed["orders"][oid]["user_id"]
    token=store.open(domain.seed["users"][uid]["email"])
    sid=store.identifier(token)
    action=(dict(tool="return_delivered_order_items",args=dict(order_id=oid,item_ids=["6777246137"],payment_method_id="gift_card_8168843"))
            if case=="D20" else dict(tool="cancel_pending_order",args=dict(order_id=oid,reason="ordered by mistake")))
    v=prepare(store,sid,[action])
    before=ledger(store,uid)
    rollback=True
    if case=="D20" and fault:
        with store.connection(True) as db:
            db.execute("CREATE TRIGGER inject BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'写回执故障'); END")
        try:
            store.execute(sid,v,"执行")
            rollback=False
        except sqlite3.IntegrityError:
            rollback=ledger(store,uid)==before and receipt_count(store)==0
        with store.connection(True) as db:
            db.execute("DROP TRIGGER inject")
    first=store.execute(sid,v,"执行")
    if case=="D21":
        if fault:
            store=Store(path,domain)
        again=store.execute(store.identifier(token),v,"执行")
        replay=again["replayed"] and again["items"]==first["items"]
    else:
        replay=True
    final=ledger(store,uid)
    order=final["orders"][oid]
    extra=len(order["payment_history"])-len(before["orders"][oid]["payment_history"])
    if case=="D21":
        ok=order["status"]=="cancelled" and extra==1 and final["users"][uid]["payment_methods"]["gift_card_7219486"]["balance"]==28899.15
    else:
        ok=order["return_items"]==["6777246137"] and order["status"]=="return requested"
    return dict(订单状态=order["status"],新增支付流水=extra,回执数量=receipt_count(store),故障回滚=rollback,重放一致=replay,
                检验符合预期=ok and rollback and replay and receipt_count(store)==1)


def archive_count(archive):
    plan=json.loads((archive/"清单.json").read_bytes())["jobs"]
    ids=[str(case) for suite,case,arm in plan if suite=="ecom" and arm=="old"]
    assert len(set(ids))==len(ids)==33
    scores=[]
    tries=0
    for case in ids:
        matches=[archive/f"结果/ecom-{case}-old/result.json",archive/f"补跑/ecom-{case}-old/result.json"]
        records=[json.loads(p.read_bytes()) for p in matches if p.exists()]
        tries+=len(records)
        valid=[r["score"] for r in records if "score" in r]
        assert len(valid)<=1
        scores+=valid
    return dict(计划=len(ids),评分=len(scores),通过=sum(scores),尝试=tries)


def score_blind(archive,reverse):
    paths=[archive/f"结果/ecom-49-{arm}/trajectory.json" for arm in ("old","full")]
    if reverse:
        paths.reverse()
    records=[]
    for p in paths:
        raw=p.read_bytes()
        trace=json.loads(raw)
        env=MockStoryEnv(task_index=49,console_verbose=RunLogger(False))
        env._create_isolated_data()
        server.cache_dir=env.cache_dir
        shop=Shop(env)
        data=shop.initial(shop.uid)
        for tool in trace["tools"]:
            f=tool["function"]
            shop.invoke(data,f["name"],json.loads(f["arguments"]))
        # 按轨迹重放写入后，由未修改的官方评分器独立检查终态、查询和输出。
        server.data=data
        original_save(data)
        env.tool_calls=trace["tools"]
        score,action,search,output,_=env.calculate_reward(trace["session"],[.001])
        records.append(dict(轨迹摘要=hashlib.sha256(raw).hexdigest(),成绩=[score,action,search,output]))
    return dict(匿名顺序=records,检验符合预期=sorted(r["成绩"] for r in records)==[[0,1,0,1],[1,1,1,1]])


def evaluate(case,index,folder,archive):
    folder.mkdir(parents=True,exist_ok=False)
    server.set_data=lambda data:None
    alternate=index==2
    if case=="D17": return version_replay(folder,alternate)
    if case=="D18": return grouped_refund(folder,alternate)
    if case=="D19": return asyncio.run(session_replay(folder,alternate))
    if case in ("D20","D21"): return tau_replay(folder,case,alternate)
    if case=="D22":
        isolated = folder/"输入"
        files = [archive/"清单.json", *archive.glob("结果/ecom-*-old/result.json"), *archive.glob("补跑/ecom-*-old/result.json")]
        for path in files:
            destination = isolated/path.relative_to(archive)
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,destination)
        retry = isolated/"补跑/ecom-31-old/result.json"
        saved = folder/"移出的唯一补跑.json"
        if alternate:
            retry.rename(saved)
        observed=archive_count(isolated)
        if alternate:
            saved.rename(retry)
        restored=archive_count(isolated)
        return dict(初读=observed,恢复=restored,检验符合预期=observed==dict(计划=33,评分=32 if alternate else 33,通过=11,尝试=33 if alternate else 34)
                    and restored==dict(计划=33,评分=33,通过=11,尝试=34))
    if case=="D23":
        name="configs/aligned_evaluation.json"
        data=(archive/name).read_bytes()
        if alternate:
            old='"重复次数": 1'.encode()
            assert data.count(old)==1
            data=data.replace(old,'"重复次数": 3'.encode())
        copy=folder/Path(name).name
        copy.write_bytes(data)
        expected=json.loads((archive/"清单.json").read_bytes())["sha256"][name]
        matched=hashlib.sha256(copy.read_bytes()).hexdigest()==expected
        return dict(校验文件=name,摘要匹配=matched,检验符合预期=matched!=alternate)
    if case=="D24": return score_blind(archive,alternate)
    raise ValueError(case)


original_save=server.set_data
