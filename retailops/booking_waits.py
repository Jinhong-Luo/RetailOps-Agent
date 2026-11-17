"""同一会话内按订单、服务和待核条件保存预约依据。"""
from .domain import Issue

import re

KINDS = ["日期", "钟点", "费用", "空间", "墙体", "联系人", "办理授权", "其他"]
SERVICES = ["安装", "维修", "检查"]
PROVIDERS = ["顾客", "业务查询", "安装方", "顾客暂缓"]


def invalid(reason):
    raise Issue("booking_wait", reason, "核对本单剩余条件和顾客最新授权")


def save(store, db, row, records, history):
    if not isinstance(records, list) or not records:
        invalid("缺少本次要登记的等待条件")
    task = store.domain.env.task
    orders = store.toolkit(db,row["uid"]).db["orders"][task.platform][task.shop_id][row["uid"]]
    for record in records:
        oid, service, kind, provider, condition, evidence = [record.get(k) for k in
            ("order_id","service","kind","provider","condition","evidence")]
        if oid not in orders or service not in SERVICES or kind not in KINDS or provider not in PROVIDERS:
            invalid("订单、服务或条件类型不符")
        if not all(isinstance(v,str) and v.strip() for v in (condition,evidence)):
            invalid("等待条件缺少事实或依据")
        if not any(evidence in (m.get("content") or "") for m in history if m["role"] in {"user","tool"}):
            invalid("未找到所引依据")
        if kind == "钟点" and (provider != "安装方" or not re.search(
                r"几点|钟点|时段|小时|点钟|上午|下午|晚上|早上|中午|[0-9一二三四五六七八九十]+点|空档|准确时间|具体时间", evidence)):
            invalid("预约工具只登记星期；只有顾客明确要求先核实具体时段时才登记安装方等待")
        db.execute("""INSERT INTO scoped_wait(sid,order_id,service,kind,provider,condition,evidence,revision)
                      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(sid,order_id,service,kind)
                      DO UPDATE SET provider=excluded.provider,condition=excluded.condition,evidence=excluded.evidence,
                      revision=excluded.revision,resolved=0""",
                   (row["sid"],oid,service,kind,provider,condition,evidence,row["seq"]))


def resolve(store, sid, seq, items, history):
    with store.connection(True) as db:
        row = store.row(db,sid)
        store.check_seq(row,seq)
        latest = next(m["content"] for m in reversed(history) if m["role"]=="user")
        if not isinstance(items,list) or not items:
            invalid("缺少本次补足的条件")
        for item in items:
            key = (sid,item.get("order_id"),item.get("service"),item.get("kind"))
            wait = db.execute("SELECT * FROM scoped_wait WHERE sid=? AND order_id=? AND service=? AND kind=?",key).fetchone()
            evidence = item.get("evidence")
            if not wait or not isinstance(evidence,str) or not evidence.strip() or wait["revision"]>=seq:
                invalid("需要登记之后的新依据")
            if evidence not in latest:
                invalid("恢复办理需要当前顾客提供的资料或授权原话")
            # 用户撤回具体时段前提，与安装方确认可到场是不同事件。
            waived_time = (wait["kind"]=="钟点" and wait["provider"]=="安装方"
                           and re.search(r"(钟点|时段|几点|时间).{0,6}(不用确认|不要求|不必确认)", evidence))
            if wait["provider"] in {"安装方","顾客暂缓"} and item.get("basis")!="重新授权" and not waived_time:
                invalid("转接或查进度不能解除安装方待核条件或暂缓")
            if item.get("basis")=="重新授权" and any(w in evidence for w in ("不同意","暂缓","先别","先不","不要预约","查进展")):
                invalid("当前原话仍在暂缓或仅查询进展")
            if item.get("basis")=="重新授权" and not any(w in evidence for w in
                    ("同意","请用","请预约","可以预约","可以安排","请安排","现在办","不用等","不用确认","不要求","撤回","按周")):
                invalid("需要顾客明确变更原条件或重新同意办理")
            db.execute("UPDATE scoped_wait SET resolved=1,revision=? WHERE sid=? AND order_id=? AND service=? AND kind=?", (seq,*key))
            store.event(db,sid,"预约补足",order_id=key[1],service=key[2],kind=key[3],evidence=evidence)
        return store.state(db,row)


def check(db,sid,args):
    if db.execute("SELECT 1 FROM booking_wait WHERE sid=?",(sid,)).fetchone():
        invalid("旧预约记录未绑定订单，需先核实原条件的归属")
    if db.execute("SELECT 1 FROM scoped_wait WHERE sid=? AND order_id=? AND service=? AND resolved=0",
                  (sid,args.get("order_id"),args.get("service_type"))).fetchone():
        invalid("本单仍有待补资料或授权，先逐项核实")
