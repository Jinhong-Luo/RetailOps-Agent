"""同一会话内按订单、服务和待核条件保存预约依据。"""
import json

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
            if item.get("basis") == "重新授权":
                pending = db.execute("SELECT DISTINCT order_id,service FROM scoped_wait WHERE sid=? AND resolved=0", (sid,)).fetchall()
                if len(pending)>1 and key[1] not in evidence:
                    invalid("有多项等待，恢复授权须引用指明本单的原话")
                # 原话可能同时授权甲单并暂缓乙单，只检查包含本次证据的完整分句。
                clauses = [part for part in re.split(r"[。；;！!，,]", latest) if evidence in part or part.strip() and part in evidence]
                if len(pending)==1 and re.search(r"暂缓|先别|别约|先不(?:约|办|提交)|不要(?:约|办|提交)", latest):
                    invalid("本轮仍含暂缓条件")
                if not clauses or any(re.search(r"不同意|暂缓|先别|先不|不要|别约|别办|别继续|不继续|不能|尚未|查进展|能否|是否|如果|假如|等.{0,6}再|可以吗|[？?]", part) for part in clauses):
                    invalid("需要已生效的办理授权")
                if not re.search(r"同意|请用|请预约|可以预约|可以安排|请安排|现在办|不用等|不用确认|不要求|撤回|按周|(?:按|照).{0,12}继续|继续(?:办理|预约|安排)", evidence):
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


def bind_legacy(store, sid, seq, oid, service, kind, evidence):
    with store.connection(True) as db:
        row = store.row(db,sid)
        store.check_seq(row,seq)
        legacy = db.execute("SELECT * FROM booking_wait WHERE sid=?", (sid,)).fetchone()
        if not legacy:
            invalid("当前没有待核实归属的旧预约条件")
        task = store.domain.env.task
        orders = store.toolkit(db,row["uid"]).db["orders"][task.platform][task.shop_id][row["uid"]]
        latest = next(m["content"] for m in reversed(json.loads(row["history"])) if m["role"]=="user")
        if (not isinstance(evidence,str) or not evidence or evidence not in latest or service not in SERVICES
                or service not in evidence or kind not in KINDS or oid not in orders
                or [order for order in orders if order in evidence] != [oid]
                or not any(evidence in clause and not re.search("不是|并非|不属于",clause)
                           for clause in re.split("[，。；,;]",latest))):
            invalid("请顾客明确旧条件属于哪一笔订单和哪项服务")
        if legacy["provider"]=="顾客暂缓":
            kind="办理授权"
        if db.execute("SELECT 1 FROM scoped_wait WHERE sid=? AND order_id=? AND service=? AND kind=?",
                      (sid,oid,service,kind)).fetchone():
            invalid("该业务已有同类等待条件，请先核实两条记录")
        db.execute("""INSERT INTO scoped_wait(sid,order_id,service,kind,provider,condition,evidence,revision)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (sid,oid,service,kind,legacy["provider"],legacy["condition"],legacy["evidence"],legacy["revision"]))
        db.execute("DELETE FROM booking_wait WHERE sid=?", (sid,))
        store.event(db,sid,"确认旧预约归属",order_id=oid,service=service,evidence=evidence)
        return store.state(db,row)
