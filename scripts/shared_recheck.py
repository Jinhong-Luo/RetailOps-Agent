"""从冻结题卡、落盘会话、独立账本和回执复核共享成绩，仅导出分项结论。"""
import argparse
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from module_manifest import build
from shared_grade import grade


def read(path):
    return json.loads(path.read_bytes())


def sources(root, key, release):
    files = list((root/"共享"/key).glob("*/已采集.json"))
    if len(files) != 1 or read(files[0])["冻结参照"] != release:
        raise ValueError("缺少唯一匹配的已采集位置：" + key)
    return files[0].parent


def connection(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


def business(case, folder):
    from shared_fixture import ORDER_IDS, ecom_case, tau_case
    original = read(folder/"逐轮证据.json")
    domain = ecom_case(case, "full") if case in ORDER_IDS else tau_case(case)[0]
    rebuilt = deepcopy(original)
    with connection(folder/"账本.db") as db:
        sessions = db.execute("SELECT sid,uid FROM sessions ORDER BY rowid").fetchall()
        rebuilt["初态"] = [domain.initial(uid) for _,uid in sessions]
        rebuilt["终态"] = [json.loads(db.execute("SELECT body FROM accounts WHERE uid=?", (uid,)).fetchone()[0]) for _,uid in sessions]
        rebuilt["成功回执"] = [dict(json.loads(body),replayed=False) for body, in db.execute("SELECT body FROM receipts ORDER BY rowid")]
        rebuilt["事件"] = [[dict(json.loads(body),id=n) for n,body in db.execute(
            "SELECT id,body FROM events WHERE sid=? ORDER BY id", (sid,))] for sid,_ in sessions]
    before = grade(case, original)
    after = grade(case, rebuilt)
    checks = {name: original[name] == rebuilt[name] for name in ("初态","终态","成功回执","事件")}
    return dict(原分=int(all(before.values())),复核分=int(all(after.values())),逐项一致=before==after,
                登记与落盘一致=checks,一致=all(checks.values()) and before==after,
                范围="使用原始可见答复和工具轨迹，独立重建初态并从数据库重读终态、回执与事件")


def fixed(case, folder, second):
    diag = read(folder/"已采集.json").get("诊断", {})
    if case=="D16":
        blind = read(folder/"盲读原始记录.json")
        reply = blind["自由复述"].replace(" ","")
        with connection(folder/"账本.db") as db:
            phase, plan = db.execute("SELECT phase,plan FROM sessions").fetchone()
            n = db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        actions = json.loads(plan)
        refund = [a for a in actions if a["tool"]=="manage_ecard_tool"]
        return dict(待确认且无执行=phase=="待确认" and n==0,同笔退款239元=len(refund)==1 and refund[0]["args"]["amount"]==239,
                    读者仍需核实原卡="原" in reply and "吗" in reply and "239" in reply,
                    隐藏对照条件=all(w not in blind["界面"] for w in ["D16-","对照组","实验组"]))
    data = folder/"隔离输入"
    if case=="D18":
        values=[]
        for complete in (True,False):
            with connection(data/str(complete)/"测试.db") as db:
                account=json.loads(db.execute("SELECT body FROM accounts").fetchone()[0])
                count=db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            orders=next(iter(next(iter(account["orders"]["jd"].values())).values()))
            order=next(iter(orders.values()))
            balance=next(iter(account["users_info"].values()))["电子卡余额"]
            values.append(dict(完整=complete,状态=order["订单状态"],余额=balance,回执=count))
        return dict(两份独立终态匹配=diag["回放"]==values,完整单仅一份回执=values[0]["回执"]==1,
                    漏退款时按开关拦截=(values[1]["回执"]==0)==second)
    dbpath=next(data.glob("*.db"))
    with connection(dbpath) as db:
        phases=[v for v, in db.execute("SELECT phase FROM sessions ORDER BY rowid")]
        stages=[json.loads(b)["stage"] for b, in db.execute("SELECT body FROM events ORDER BY id")]
        receipts=[json.loads(b) for b, in db.execute("SELECT body FROM receipts ORDER BY rowid")]
        account=json.loads(db.execute("SELECT body FROM accounts LIMIT 1").fetchone()[0])
    if case=="D17":
        return dict(新版仅一份加急回执=len(receipts)==1 and [i["tool"] for i in receipts[0]["items"]]==["manage_urgent_tool"],
                    两版草案落盘=stages.count("待确认")==2,
                    原按钮检查记录区分两组=diag["迟到确认被接受"]==second and diag["授权业务检查通过"]!=second)
    if case=="D19":
        return dict(双会话无写入=len(phases)==2 and not receipts,
                    取消事件与条件相符=("取消" in stages)==second,乙请求完成=diag["乙状态"]==200,
                    完成输出数量匹配=stages.count("输出解析")==2-int(second))
    from retailops.domain import Domain
    domain=Domain()
    oid="#W4817420" if case=="D20" else "#W6779827"
    original=domain.seed["orders"][oid]
    now=account["orders"][oid]
    checks=dict(唯一回执=len(receipts)==diag["回执数量"]==1,终态复核=now["status"]==diag["订单状态"],
                支付流水独立计数=len(now["payment_history"])-len(original["payment_history"])==diag["新增支付流水"])
    if case=="D20":
        checks.update(退货商品匹配=now["return_items"]==["6777246137"],初次返回情况按原注入记录核对=diag["故障回滚"])
    else:
        uid=original["user_id"]
        checks.update(重放事件出现="重放回执" in stages,礼品卡余额正确=account["users"][uid]["payment_methods"]["gift_card_7219486"]["balance"]==28899.15)
    return checks


def run(source, root, release):
    manifest=build(source)
    result={}
    for row,cells in manifest["矩阵"].items():
        if not row.startswith(("D","S")):
            continue
        scoring=cells["评测流程本身"]["位置"]
        if len(scoring)!=1:
            continue
        if row+"__业务对话__full" in manifest["位置"]:
            if not list((root/"共享"/(row+"__业务对话__full")).glob("*/已采集.json")):
                continue
            folder=sources(root,row+"__业务对话__full",release)
            result[row]=business(row,folder)
        else:
            arms=[]
            for index in (1,2):
                key=manifest["专题"][f"{row}-{index}"]
                arms.append(fixed(row,sources(root,key,release),index==2))
            result[row]=dict(一致=all(all(c.values()) for c in arms),两个条件核验=arms,
                             范围="重读原盲读或故障记录并核对数据库终态。瞬时按钮返回和回滚时刻取自原采集记录。")
    if len(result)>27:
        raise ValueError("共享独立评分位置数超过二十七")
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("source","root","output"):
        parser.add_argument(name,type=Path)
    parser.add_argument("release")
    args=parser.parse_args()
    rows=run(args.source,args.root,args.release)
    args.output.write_text(json.dumps(dict(服务提交=args.release,逐题=rows),ensure_ascii=False,indent=2)+"\n")
    print("已复核",len(rows),"一致",sum(r["一致"] for r in rows.values()))
    for k,r in rows.items():
        if not r["一致"]:
            checks=r.get("两个条件核验",[r.get("登记与落盘一致",{})])
            print(k,[[name for name,v in c.items() if not v] for c in checks])
