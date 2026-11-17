"""按封存脚本投递当轮台词和当前版确认，保存面客回复、真实工具、初终账本与计量。"""

import json
import os
import time
from types import SimpleNamespace

from ecom_paired import Agent, ButtonService, Issue, Store, confirmed_reply, dump, server
from shared_fixture import ORDER_IDS, OLD, NEW, ecom_case, tau_case
from aligned_trial import configure
import httpx


def read_account(store, sid):
    with store.connection() as db:
        uid = store.row(db,sid)["uid"]
        return json.loads(db.execute("SELECT body FROM accounts WHERE uid=?",(uid,)).fetchone()[0])


def fits(actions, wants):
    if len(wants) != len(actions):
        return False
    unused = list(actions)
    for name, fields in wants:
        hit = next((a for a in unused if a["tool"]==name and all(
            a["args"].get(k)==v for k,v in fields.items())),None)
        if hit is None:
            return False
        params = hit["args"]
        if name == "manage_order_tool" and any(params.get(k) and k not in fields for k in ("address","phone_number","product_info_list")):
            return False
        if name == "manage_ecard_tool" and (params.get("order_id","甲单")!="甲单" or params.get("refund_reason","取消")!="取消"):
            return False
        unused.remove(hit)
    return True


def expected(case, index):
    def urgent(oid):
        return "manage_urgent_tool", dict(order_id=oid)
    def invoice(oid,title,num,kind):
        return "manage_invoice_tool",dict(order_id=oid,title=title,phone_number=num,invoice_type=kind)
    def order(oid,act,**fields):
        return "manage_order_tool",dict(order_id=oid,action=act,**fields)
    def booking(oid,service,day,name,phone):
        return "schedule_service_tool",dict(order_id=oid,service_type=service, service_time=day,
                                           user_name=name,phone_number=phone)
    table = {
        ("D01",0):[urgent("冻青01"),invoice("冻青02","海棠书店","13000001002","企业发票"),
                   order("冻青03","修改",phone_number="13000001003")],
        ("D02",0):[urgent("湖蓝11")], ("D02",1):[urgent("湖蓝12")],
        ("D03",0):[order("云杉21","修改",address="云杉路12号"),invoice("云杉21","周青","13000001021","个人发票")],
        ("D03",1):[order("云杉21","修改",address="云杉路12号")],
        ("D04",0):[urgent("青檀31")], ("D06",1):[order("海蓝41","修改",phone_number="13000001041")],
        ("D07",0):[booking("竹影51","检查","周四","杜禾","13000001051")],
        ("D08",2):[booking("松墨61","维修","周一","郭宁","13000001061")],
        ("S07",1):[invoice("乙单","青禾商贸","13000000007","企业发票")],
        ("S08",1):[booking("甲单","维修","周三","林安","13000000008")],
        ("S09",0):[("manage_exchange_tool",dict(order_id="313672076398",action="换货",original_product_id=OLD,exchange_product_id=NEW))],
        ("S11",2):[order("甲单","取消"),("manage_ecard_tool",dict(action="退款",amount=239))],
        ("S12",1):[("cancel_pending_order",dict(order_id="#W4386313",reason="no longer needed"))],
    }
    return table.get((case,index))


def address_match(actions, oid, suffix, code):
    if len(actions)!=1 or actions[0]["tool"]!="modify_pending_order_address":
        return False
    a=actions[0]["args"]
    return a.get("order_id")==oid and a.get("zip")==code and suffix in (a.get("address1","")+a.get("address2",""))


def prefix(store, sid, case, first):
    if case == "D14":
        uid = store.snapshot(sid)[0]["user_id"]
        args = dict(platform="jd",shop_id="合成店",user_id=uid,order_id="甲单")
        acts = [dict(tool="manage_order_tool",args=dict(args,action="取消")),
                dict(tool="manage_ecard_tool",args=dict(args,action="退款",amount=168,refund_reason="取消"))]
        msg = "第1版：取消甲单，168元退回原京东E卡。请核对后确认。"
        store.finish(sid,0,actions=acts,messages=[dict(role="assistant",content=msg)])
    if case in {"S09","S10","S12"}:
        if case=="S09":
            mapping="本次顾客把订单313672076398称为甲单。乙单在账本中编号为乙单。"
        elif case=="S12":
            mapping="本次顾客说的甲单编号为#W4386313。"
        else:
            own = next(iter(read_account(store,sid)["orders"]))
            mapping="该窗口的顾客将本人唯一订单称为"+("甲单" if own.endswith("1") else "乙单")+"。"
        store.finish(sid,0,messages=[dict(role="system",content=mapping)])
    if case != "D10":
        return
    order = store.read(sid,"get_order_details",dict(order_id="#W4277243"))
    paid = order["payment_history"]
    def amount(kind):
        return sum(float(tx["amount"]) for tx in paid if tx["transaction_type"]==kind)
    rows = [("帮我查 #W4277243 现在什么状态",order["status"]),("这笔已取消的单原来花了多少钱",str(amount("payment"))),
            ("退了多少钱",str(amount("refund"))),("收货城市是哪里",order["address"]["city"])]
    history=[dict(role="user",content=first),dict(role="assistant",content="知道了，只在本次对话里使用。")]
    for n,(query,answer) in enumerate(rows):
        args=dict(order_id="#W4277243")
        actual=store.read(sid,"get_order_details",args)
        history.extend([dict(role="user",content=query),dict(role="assistant",content=None,tool_calls=[dict(
            id=str(n),type="function",function=dict(name="get_order_details",arguments=dump(args)))]),
            dict(role="tool",tool_call_id=str(n),content=dump(actual)),dict(role="assistant",content=answer)])
    assert "露营箱" not in dump(history[2:]) and "#W4386313" not in dump(history[2:])
    store.finish(sid,0,messages=history)


FOLLOW = {"D02":["可以催了。"], "D03":["发票那件事撤掉，另一件照原先说的办，重新列一下。"],
    "D06":["新号13000001041，请只改这单。"],"D08":["我还拿不到他的号码，先保留周一维修的需求，别提交。",
    "联系郭宁，13000001061，按刚才那单继续。"],"D09":["有师傅是否十点前到的确切答复了吗？",
    "我先不安装了，即使今天有消息也先别登记；等我再联系。"], "D14":["那我现在不取消了。"]}


async def run(spec, arm, folder):
    case = spec["编号"]
    configure(arm if arm in {"no_memory","human","raw","heavy_wait","no_wait","no_consult","no_needs"} else "full")
    os.environ["RETAILOPS_ALIGNED_ARM"]=arm
    if arm in {"short_no_recall","short_history"}:
        os.environ["RETAILOPS_REFERENCE"]="无记忆"
    server.set_data=lambda data:None
    if case in ORDER_IDS:
        domain=ecom_case(case,arm)
        identities=[domain.uid]
    else:
        domain,identities=tau_case(case)
        if case not in {"S10","S12"}:
            identities=identities[:1]
    store=Store(folder/"账本.db",domain)
    sessions=[store.identifier(store.open(identity)) for identity in identities]
    initial=[read_account(store,sid) for sid in sessions]
    for sid in sessions:
        prefix(store,sid,case,spec["顾客首句"])
    turns,receipts=[],[]
    follow = [s["顾客原话"] for s in spec["后续步骤"]] if case.startswith("S") else FOLLOW.get(case,[])
    sequence = [spec["顾客首句"],*follow]
    if case=="D10":
        sequence=["刚才我怎么称呼 #W4386313 来着？"]

    class Client(httpx.AsyncClient):
        async def post(self, path, *, json):
            start=time.monotonic()
            with (folder/"模型输入.jsonl").open("a") as f:
                f.write(dump(json)+"\n")
            result=await super().post(path,json=dict(json,seed=300))
            result.raise_for_status()
            body=result.json()
            with (folder/"模型回执.jsonl").open("a") as f:
                f.write(dump(dict(消息=body["choices"][0]["message"],用量=body.get("usage"),元数据=body.get("bridge_metadata"),秒=time.monotonic()-start))+"\n")
            return result

    def accept(sid,v,label):
        ButtonService.click(SimpleNamespace(store=store,sid=sid,version=v),v)
        receipt=store.execute(sid,v,label)
        receipts.append(receipt)
        return confirmed_reply(store,receipt) if case in ORDER_IDS else "已按您确认的方案完成操作。"

    try:
        async with Client(base_url="http://127.0.0.1:18863/v1/",timeout=300,trust_env=False,
                headers={"Authorization":"Bearer "+os.environ["CODEX_BRIDGE_TOKEN"]}) as client:
            agent=Agent(store,client,timeout=600)
            if case in ORDER_IDS:
                agent.schemas=[s for s in agent.schemas if s["function"]["name"]!="find_variants"]
            for n, raw in enumerate(sequence):
                sid=sessions[1 if raw.startswith("B：") else 0]
                line=raw.removeprefix("A：").removeprefix("B：")
                state=store.snapshot(sid)[0]
                if case in {"S10","S12"} and n==2:
                    # 顾客在甲窗口直接按原草案按钮，防止用文字轮次替换迟到的真实确认。
                    row=dict(顾客=line,输入方式="绑定旧窗口的确认按钮",版本=state["version"])
                    try:
                        row["面客回执"]=accept(sid,state["version"],"甲窗口")
                    except (Issue,ValueError) as exc:
                        row["拒绝"]=exc.data if isinstance(exc,Issue) else str(exc)
                    if case=="S12" and receipts:
                        v=store.snapshot(sessions[1])[0]["version"]
                        row["取消回执重放"]=store.execute(sessions[1],v,"确认1")["replayed"]
                    turns.append(row)
                    break
                try:
                    result=await agent.run(sid,line,state["revision"],"B",str(n))
                except Issue as exc:
                    if exc.data["code"] != "step_limit":
                        raise
                    turns.append(dict(顾客=line,停止原因="已用完本轮八步",阶段="业务失败"))
                    break
                state=result["state"]
                acts=state["actions"] if state["phase"]=="待确认" else []
                screen=result["reply"]
                if acts:
                    screen+=dump(acts)+ButtonService.confirmation_prompt(SimpleNamespace(version=state["version"]))
                row=dict(顾客=line,面客答复=screen,草案=acts,状态=state,执行前账本=read_account(store,sid))
                turns.append(row)
                wants=expected(case,n)
                correct=fits(acts,wants) if wants is not None else False
                if case=="S10":
                    correct=address_match(acts,["#W9900111","#W9900112"][n],["11号201室","22号302室"][n],["310000","215000"][n])
                if case=="S12" and n==0:
                    correct=address_match(acts,"#W4386313","31号401室","650000")
                if case=="S11" and n==0:
                    correct=fits(acts,expected("S11",2)) and "239" in screen and "退" in screen
                if case=="S07" and n==1:
                    # 顾客核对正确乙单草案后才说同意，随后点击同一版。
                    if correct:
                        row["确认前顾客原话"]=sequence[2]
                        row["面客回执"]=accept(sid,state["version"],"确认乙单")
                    break
                if correct and (case,n) not in {("D03",0),("S10",0),("S12",0),("S11",0)}:
                    row["面客回执"]=accept(sid,state["version"],"确认"+str(n))
                gated = ((case,n) in {("D02",0),("D03",0),("S08",1),("S09",0),("S10",0),("S10",1),
                                      ("S11",0),("S12",0),("S12",1)})
                if gated and not correct:
                    row["停止原因"]="到达需要正确草案或成功回执的节点，未满足继续条件"
                    break
    finally:
        private=dict(初态=initial,轮次=turns,成功回执=receipts,终态=[read_account(store,sid) for sid in sessions],
                     事件=[store.events(sid,0) for sid in sessions],工具=getattr(getattr(domain,"env",None),"tool_calls",[]))
        (folder/"逐轮证据.json").write_text(json.dumps(private,ensure_ascii=False,indent=2),encoding="utf8")
    return private
