"""冻结的新案例、脚本顾客、按钮和独立账本评分；每个试次一份私有记录。"""
import argparse
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from experience_fixture import ExperimentShop
from fastapi.testclient import TestClient
from retailops.api import create_app
from retailops.experience import Experience

ARMS = ("无记忆","人工提示","历史检索","提炼记忆")
U, I = "manage_urgent_tool","manage_invoice_tool"


def case(index, d):
    a,b = d.orders
    title = ("禾舟","浅湾","青谷","松屿","竹屿","平沙","林渡","川梧")[index]
    urgent = dict(tool=U,args=dict(platform="jd",shop_id=d.shop,user_id=d.uid,order_id=a))
    invoice = dict(tool=I,args=dict(order_id=b if index==1 else a, invoice_type="个人发票",
                                    title=title,phone_number=d.contact))
    contact = "联系电话用你查到的账户号码"
    # 每个元素是这一轮的话、允许按钮点击的完整草案、纯回复须表达的条件。
    plans = [
        [(f"{a}麻烦催快一点，同时开个人发票，抬头用{title}，{contact}。",[urgent,invoice],())],
        [(f"两笔分开处理：{a}催发货，{b}开个人发票，写{title}，{contact}。",[urgent,invoice],())],
        [(f"{a}请加急。另外能开个人发票吗？票我只打听一下。",[urgent],("可以","支持","能开","可开"))],
        [(f"{a}要个人发票，{contact}。抬头等我给，先问我。",[],("抬头",)),
         (f"就写{title}，可以出草案了。",[invoice],())],
        [(f"{a}打算开个人发票，{contact}。先告诉我用什么抬头合适，我同意后才能拟案。",[],("抬头","姓名")),
         (f"决定了，抬头写{title}，现在提交草案。",[invoice],())],
        [(f"{a}的加急跟开票都先放一放，不用拟方案。",[],("暂","不","暂停","搁置")),
         ("现在只恢复催发货，票先放着。",[urgent],())],
        [(f"{a}先加急，发票我只是问问，能不能开？",[urgent],("可以","支持","能开","可开"))],
        [(f"请为{a}加急并开个人发票，发票写{title}，{contact}。",[urgent,invoice],())],
    ]
    return plans[index]


def equal(left,right):
    return sorted(json.dumps(a,sort_keys=True) for a in left)==sorted(json.dumps(a,sort_keys=True) for a in right)


def run(job):
    folder,index,arm,repeat = job
    out=folder/f"{index}-{repeat}-{arm}"
    out.mkdir(parents=True,exist_ok=False)
    d=ExperimentShop(300+index)
    script=case(index,d)
    app=create_app(out/"business.db",domain=d,timeout=180)
    start=time.monotonic()
    rows=[]
    corrections=0
    error=None
    with TestClient(app) as client:
        app.state.agent.experience=Experience(arm)
        store=app.state.store
        token=client.post("/sessions",json=dict(email=d.uid)).json()["token"]
        client.headers["Authorization"]="Bearer "+token
        sid=store.identifier(token)
        for turn in range(4):
            k=min(turn,len(script)-1)
            message,desired,keywords=script[k]
            if turn>=len(script):
                if not desired:
                    break
                corrections+=1
                message="请按我刚才明确的范围重新核对。"+message
            state=client.get("/state").json()
            response=client.post("/messages",json=dict(message=message,mode="B",revision=state["revision"],request_id=str(turn)))
            body=response.json()
            rows.append(dict(message=message,body=body))
            if response.status_code!=200:
                error=body.get("code",str(response.status_code))
                break
            state=body["state"]
            actions=state["actions"] if state["phase"]=="待确认" else []
            ok=equal(actions,desired)
            rows[-1]["decision"]=ok and (not keywords or any(k in body["reply"] for k in keywords))
            if desired and ok:
                v=state["version"]
                client.post("/confirm",json=dict(version=v)).raise_for_status()
                rows[-1]["receipt"]=client.post("/execute",json=dict(version=v,request_id="按钮")).json()
                break
        with store.connection() as db:
            data=store.toolkit(db,d.uid).db
        wanted=script[-1][1]
        orders=data["orders"]["jd"][d.shop][d.uid]
        expedited={oid for oid,o in orders.items() if o["是否加急"]}
        inv={a["args"]["order_id"]:{"订单ID":a["args"]["order_id"],"发票类型":a["args"]["invoice_type"],
             "发票抬头":a["args"]["title"],"手机号码":a["args"]["phone_number"]} for a in wanted if a["tool"]==I}
        expected={a["args"]["order_id"] for a in wanted if a["tool"]==U}
        events=store.events(sid,0)
        calls=[e for e in events if e["stage"]=="模型往返"]
        _,history=store.snapshot(sid)
        valid=all(e["model"]=="gpt-6.1-sol" and e["effort"]=="medium" and e["usage"] for e in calls)
        queries=[(c["function"]["name"],c["function"]["arguments"]) for m in history for c in m.get("tool_calls",[])
                 if c["function"]["name"].startswith("get_")]
        result=dict(index=index,repeat=repeat,arm=arm,first=bool(rows[0].get("decision")),final=expedited==expected and data["invoices"]==inv,
                    error=error,wrong_write=bool(expedited-expected or any(inv.get(k)!=v for k,v in data["invoices"].items())),
                    turns=len(rows),corrections=corrections,duplicate_queries=len(queries)-len(set(queries)),
                    seconds=round(time.monotonic()-start,2),calls=len(calls),metadata_ok=bool(calls) and valid,
                    input=sum(e["usage"]["prompt_tokens"] for e in calls),output=sum(e["usage"]["completion_tokens"] for e in calls))
        (out/"private.json").write_text(json.dumps(dict(rows=rows,history=history,events=events),ensure_ascii=False))
        (out/"score.json").write_text(json.dumps(result,ensure_ascii=False))
        print(out.name,flush=True)
        return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=["demo","formal"])
    parser.add_argument("folder",type=Path)
    args=parser.parse_args()
    jobs=[(args.folder,i,"提炼记忆",0) for i in (6,7)] if args.mode=="demo" else [
        (args.folder,i,ARMS[(j+i+r)%4],r) for r in range(2) for i in range(6) for j in range(4)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(run,jobs))
