"""在原服务器固定八组决策前事实，对照关闭、文件目录和已验证经验。"""
import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import time

from common import ROOT, load_env
from ecom_development import fixture
from ecom_paired import Agent, Store, enable_product, enable_refund, server
from retailops.booking_waits import resolve
from retailops.domain import Issue
from retailops.memory_budget import BudgetedAsyncClient, BudgetedClient, BudgetStopped, ModelBudget
from retailops.memory_retrieval import applicable
from retailops.official_memory import validate_wiki
from retailops.verified_memory import Archive, RULES, digest, empty, extract, learn, organize, read, write

ARMS = ("关闭", "文件目录", "已验证经验")
LIMITS = dict(calls=145, tokens=1500000, seconds=5400, request_tokens=120000)
CASES = {
    "预约补齐": "甲单71001周三可以维修，请用本单已有的联系人和电话安排，给我待确认草案。乙单71002安装仍等安装方明确几点上门，暂不约乙单。",
    "预约暂缓": "乙单71002安装先不约，即使有上门时间也暂缓。请告诉我还有什么没确认。",
    "个人开票": "请给71001开个人发票，抬头用已查到的测试客户，联系电话用该订单预留号码，资料我确认了，请给待确认方案。",
    "企业开票": "给订单71001开企业发票，抬头为测试商贸，联系电话是00000000001，我确认过资料了，请给待确认方案。",
    "关联退款": "71001已签收，我要退货并原路退回京东E卡。这笔退款我同意，先给我完整的退货和退款草案。",
    "拒绝退款": "71001的商品我原本想退货，但我不同意京东E卡退款。两件事都先不办，也不要拆开，请说明核对到的订单金额和支付方式。",
    "独立价保": "我申请订单71001的京东E卡价保退差价，商品860001，请核对当前价格，给我待确认草案。",
    "换货后型号": "甲单71001换来的新款有什么赠品？我只咨询新款，不要做任何办理。",
}


def account(store, sid):
    with store.connection() as db:
        row = store.row(db, sid)
        return deepcopy(store.toolkit(db, row["uid"]).db)


def seed(folder, case):
    folder.mkdir(parents=True, exist_ok=False)
    shop = fixture()
    initial = deepcopy(shop.initial(shop.uid))
    orders = initial["orders"]["jd"]["开发店"][shop.uid]
    products = initial["products"]["jd"]["开发店"]
    for product in products.values():
        product.update(商品状态="已上架", 商品描述="用于开发验证的恒温机", 库存=5, 商品库存=5)
    products["860001"]["可换货商品"] = ["860002"]
    products["860002"]["赠品信息"] = "赠专用清洁刷一支"
    if case in ("关联退款", "拒绝退款"):
        orders["71001"]["订单状态"] = "已签收"
    if case == "独立价保":
        products["860001"]["商品价格"] = 148
    if case in ("预约补齐", "预约暂缓"):
        for order in orders.values():
            order["订单状态"] = "已签收"
    shop.initial = lambda uid: deepcopy(initial)
    enable_product(shop)
    enable_refund(shop)
    shop.scoped_waits = True
    server.set_data = lambda data: None
    store = Store(folder / "账本.db", shop)
    sid = store.identifier(store.open(shop.uid))
    args = dict(platform="jd", shop_id="开发店", user_id=shop.uid, order_id="71001")
    if case == "换货后型号":
        action = dict(tool="manage_exchange_tool", args=dict(args, action="换货",
                      original_product_id="860001", exchange_product_id="860002"))
        state = store.propose(sid, 0, [action])
        store.confirm(sid, state["version"])
        receipt = store.execute(sid, state["version"], "前缀换货")
        latest = store.snapshot(sid)[0]
        store.finish(sid, latest["revision"], messages=[
            dict(role="system", content="已核对本会话执行回执，下面是实际换货结果：" + json.dumps(receipt, ensure_ascii=False))
        ])
    if case.startswith("预约"):
        text = "甲单71001想维修，日期我再定。乙单71002想安装，必须先确认安装方到底几点上门，没确认就不要约。"
        state, _ = store.begin_turn(sid, text, store.snapshot(sid)[0]["revision"], "固定等待")
        waits = [
            dict(order_id="71001", service="维修", kind="日期", condition="等顾客提供维修日",
                 provider="顾客", evidence="日期我再定"),
            dict(order_id="71002", service="安装", kind="钟点", condition="等安装方确认具体上门时间",
                 provider="安装方", evidence="必须先确认安装方到底几点上门"),
        ]
        store.finish(sid, state["revision"], booking={"waits": waits},
                     messages=[dict(role="assistant", content="目前两单都还有条件待确认。"),
                               dict(role="assistant", content="甲单等您提供维修日期，乙单等安装方确认上门时间。")])
    return store, sid, args


def expected(case, args):
    def action(tool, **fields):
        return dict(tool=tool, args=dict(args, **fields))
    refund = dict(tool="manage_ecard_tool", args=dict(
        platform=args["platform"], user_id=args["user_id"], order_id=args["order_id"], action="退款"
    ))
    if case == "关联退款":
        return [action("manage_return_tool"), dict(refund, args=dict(refund["args"], amount=168, refund_reason="退货"))]
    if case == "独立价保":
        return [dict(refund, args=dict(refund["args"], amount=20, refund_reason="价保"))]
    if case in ("个人开票", "企业开票"):
        return [action("manage_invoice_tool", invoice_type=case.replace("开票", "发票"),
                       title="测试客户" if case == "个人开票" else "测试商贸", phone_number="00000000001")]
    if case == "预约补齐":
        return [action("schedule_service_tool", service_type="维修", service_time="周三",
                       user_name="测试客户", phone_number="00000000001")]
    return []


def fits(actual, wanted):
    return len(actual) == len(wanted) and all(
        any(a["tool"] == w["tool"] and all(a["args"].get(k) == v for k, v in w["args"].items()) for a in actual)
        for w in wanted)


def execute(store, sid, case, state):
    store.confirm(sid, state["version"])
    receipt = store.execute(sid, state["version"], "固定确认")
    assert receipt["items"]
    if case in ("独立价保", "关联退款"):
        expected_balance = 52 if case == "独立价保" else 200
        data = account(store, sid)
        assert data["users_info"][store.domain.uid]["电子卡余额"] == expected_balance
        orders = data["orders"]["jd"]["开发店"][store.domain.uid]
        assert orders["71001"]["订单状态"] == ("已付款" if case == "独立价保" else "已退货")
        assert orders["71002"]["订单状态"] == "已付款"
        store.execute(sid, state["version"], "固定确认")
        assert account(store, sid) == data
    if case == "预约补齐":
        waits = store.snapshot(sid)[0]["booking_waits"]
        assert any(w["order_id"] == "71002" and not w["resolved"] for w in waits)
        assert not any(item["args"].get("order_id") == "71002" for item in state["actions"])
    return receipt


def verified_fixture(root, version):
    archive = Archive(root / "开发快照")
    bank = empty(version)
    archive.publish(bank)
    observations = []
    for case, text in CASES.items():
        print("核对离线证据", case, flush=True)
        store, sid, args = seed(root / "离线回放" / case, case)
        before = account(store, sid)
        state, history = store.begin_turn(sid, text, store.snapshot(sid)[0]["revision"], "同一决策点")
        if case == "预约补齐":
            state = resolve(store, sid, state["revision"], [
                dict(state["booking_waits"][0], evidence="甲单71001周三可以维修", basis="已补资料")
            ], history)
        actions = expected(case, args)
        if actions:
            state = store.propose(sid, state["revision"], actions)
            assert account(store, sid) == before
            execute(store, sid, case, state)
        elif case == "换货后型号":
            value = store.read(sid, "get_gift_info_tool", {
                "platform": "jd", "shop_id": "开发店", "product_id": "860002"
            })
            assert "清洁刷" in json.dumps(value, ensure_ascii=False)
            assert account(store, sid) == before
        else:
            assert not state["actions"] and account(store, sid) == before
        # 该标记来自上述固定正反例，生产轨迹须由对应行为检查生成同样的证据。
        event = {"规则": case, "核验": "通过", "契约": version,
                 "离线账本摘要": digest(account(store, sid))}
        observations.append(event)
        bank = learn(bank, [event], [{"规则": case, "证据": [0]}])
        archive.publish(bank)
    snapshot_path = archive.publish(organize(bank, 0))
    wiki = root / "同内容文件目录"
    wiki.mkdir()
    index = ["# 经验目录", "", "## 业务办理"]
    for n, (name, note) in enumerate(RULES.items(), 1):
        slug = f"note-{n}"
        index.append(f"[[{slug}]] — {note.condition}")
        (wiki / (slug + ".md")).write_text(
            f"---\nname: {slug}\ndescription: {note.condition}\n---\n\n{note.action}\n",
            encoding="utf-8")
    (wiki / "_index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    validate_wiki(wiki)
    write(root / "开发证据.json", observations)
    return snapshot_path, wiki


def bridge_args(timeout):
    port = int(os.environ.get("RETAILOPS_VERIFIED_BRIDGE_PORT", "18863"))
    if not 1 <= port <= 65535:
        raise ValueError("实验桥接端口超出范围")
    return dict(base_url=f"http://127.0.0.1:{port}/v1/", timeout=timeout, trust_env=False,
                headers={"Authorization": "Bearer " + os.environ["CODEX_BRIDGE_TOKEN"]})


async def replay(case, arm, folder, snapshot_path, wiki, version, budget):
    os.environ.update(RETAILOPS_REFERENCE={"关闭": "无记忆", "文件目录": "官方移植",
                      "已验证经验": "已验证经验"}[arm], RETAILOPS_MODEL="gpt-6.1-sol@medium",
                      RETAILOPS_ALIGNED_ARM="full", RETAILOPS_BOOKING_MODE="轻量",
                      RETAILOPS_OFFICIAL_STORE=str(wiki), RETAILOPS_VERIFIED_BANK=str(snapshot_path),
                      RETAILOPS_MEMORY_CONTRACT_VERSION=version)
    os.environ.pop("RETAILOPS_MEMORY_CONTRACT", None)
    store, sid, args = seed(folder, case)
    before = account(store, sid)
    start_state, prefix = store.snapshot(sid)
    initial = digest(before)
    prefix_hash = digest(prefix)
    result = None
    checks = {}
    error = None

    class Client(BudgetedAsyncClient):
        async def post(self, path, *, json):
            response = await super().post(path, json=dict(json, seed=300))
            response.raise_for_status()
            meta = response.json().get("bridge_metadata", {})
            if (meta.get("actual_model"), meta.get("reasoning_effort")) != ("gpt-6.1-sol", "medium"):
                raise ValueError("客服实际模型不匹配")
            return response

    started = time.monotonic()
    try:
        async with Client(budget, "客服", **bridge_args(120)) as client:
            agent = Agent(store, client, timeout=300)
            result = await agent.run(sid, CASES[case], start_state["revision"], "B", "固定决策点")
        state = result["state"]
        actions = state["actions"] if state["phase"] == "待确认" else []
        checks["确认前账本一致"] = before == account(store, sid)
        wanted = expected(case, args)
        checks["草案正确"] = fits(actions, wanted)
        if wanted and all(checks.values()):
            execute(store, sid, case, state)
            checks["确认后按方案完成"] = True
        reply = result["reply"]
        if case == "预约补齐":
            checks["乙单仍等安装方确认时间"] = "乙单" in reply and "安装方" in reply and any(x in reply for x in ("时间", "钟点"))
        if case == "预约暂缓":
            checks["顾客可见暂缓"] = any(word in reply for word in ("暂", "不会", "不安排", "不预约", "先不"))
        if case == "拒绝退款":
            checks["金额和原支付方式"] = "168" in reply and "京东E卡" in reply
        if case == "换货后型号":
            checks["新款赠品"] = "清洁刷" in reply and "回收旧机" not in reply
    except Exception as exc:
        error = exc.data["code"] if isinstance(exc, Issue) else type(exc).__name__
        checks["运行成功"] = False
    events = store.events(sid, 0)
    lookups = [e for e in events if e["stage"] == "经验检索"]
    injected = [e for e in events if e["stage"] == "已送入经验"]
    refs = [e for e in events if e["stage"] == "官方记忆读取"]
    checks["预算内"] = max((e.get("step", 0) for e in events), default=0) <= 8 and error != "step_limit"
    checks["开关一致"] = (not lookups and not injected if arm != "已验证经验" else not refs)
    if arm == "关闭":
        checks["开关一致"] &= not refs
    model = [e for e in events if e["stage"] == "模型往返"]
    edge = injected[0].get("next_tool") if injected else None
    write(folder / "私有结果.json", dict(实际结果=result, 完整事件=events))
    row = dict(案例=case, 条件=arm, 分项=checks, 通过=all(checks.values()),
               异常=error, 起始账本=initial, 固定前缀=prefix_hash,
               命中=lookups[0]["hits"] if lookups else [], 实际提供=bool(injected), 提供后首工具=edge,
               文件读取=len(refs), 正文读取=sum(e["tool"] == "memory_read" and not
                   (e.get("path") or "").endswith("/_index.md") for e in refs),
               模型响应=len(model), 秒=round(time.monotonic() - started, 2))
    write(folder / "结果.json", row)
    return row


def main(args):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if commit != args.commit:
        raise ValueError("工作树与冻结提交不符")
    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    version = commit
    snapshot_path, wiki = verified_fixture(root, version)
    matrix = [(case, arm) for case in CASES for arm in ARMS]
    write(root / "清单.json", dict(提交=commit, 预算=LIMITS, 位置数=len(matrix),
          开发记忆=digest(read(snapshot_path)), 位置=matrix, 输入摘要=digest(CASES),
          桥接端口=int(os.environ.get("RETAILOPS_VERIFIED_BRIDGE_PORT", "18863"))))
    if args.mode == "离线":
        print("离线证据回放通过，八类规则与二十四个位置已核对。", flush=True)
        return
    load_env()
    budget = ModelBudget(root / "预算.db", LIMITS)
    rows = []
    try:
        # 先测实际模型和 token；顺便验证提炼确实经桥接返回受约束候选。
        with BudgetedClient(budget, "提炼预检", **bridge_args(120)) as client:
            update = extract(client, read(root / "开发证据.json")[:1], empty(version))
            write(root / "提炼预检.json", dict(已发布=sorted(update["经验"]), 待验证=sorted(update["待验证"])))
        for number, (case, arm) in enumerate(matrix, 1):
            if budget.summary()["停止原因"]:
                break
            row = asyncio.run(replay(case, arm, root / f"{number:02d}", snapshot_path, wiki, version, budget))
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if row["异常"] in ("BudgetStopped", "HTTPStatusError", "ReadTimeout", "ConnectError"):
                break
    finally:
        paired = all(len({(r["起始账本"],r["固定前缀"]) for r in rows if r["案例"]==case}) <= 1 for case in CASES)
        write(root / "汇总.json", dict(冻结提交=commit, 计划=len(matrix), 完成=len(rows), 起始一致=paired,
              预算=budget.summary(), 结果=rows))
    if len(rows) != len(matrix) or not paired:
        raise RuntimeError("保留已完成位置，实验未完成或起始事实不一致")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("离线", "实跑"))
    parser.add_argument("root", type=Path)
    parser.add_argument("commit")
    main(parser.parse_args())
