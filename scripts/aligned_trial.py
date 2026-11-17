"""在独立进程、独立账本中执行一项配对试次，输出脱敏统计。"""
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from aligned_manifest import CONFIG, verify_seal
from common import ROOT, UPSTREAM, load_env, read_json, write_json


def configure(arm):
    if arm not in read_json(CONFIG)["条件"]:
        raise ValueError("未知实验条件")
    load_env()
    os.environ.update(RETAILOPS_ALIGNED_ARM=arm, RETAILOPS_MODEL="gpt-6.1-sol@medium",
                      RETAILOPS_REFERENCE={"no_memory":"无记忆", "human":"人工提示", "raw":"历史检索"}.get(arm, "提炼记忆"),
                      RETAILOPS_BOOKING_MODE="完整" if arm == "heavy_wait" else "轻量",
                      OPENAI_API_BASE="http://127.0.0.1:18863/v1", OPENAI_API_KEY=os.environ["CODEX_BRIDGE_TOKEN"])


def usage(rows):
    values = [r["usage"] for r in rows if r.get("usage")]
    return dict(calls=len(rows), metered=len(values), input=sum(r["prompt_tokens"] for r in values),
                output=sum(r["completion_tokens"] for r in values),
                verified=bool(rows) and all(r.get("model")=="gpt-6.1-sol" and r.get("effort")=="medium" for r in rows))


def tau(case, out):
    from run_baseline import child_env, make_command
    cfg = read_json(ROOT / "configs/customer_button.json")
    cfg.update(num_trials=1, max_retries=0, max_concurrency=1)
    env = child_env(cfg)
    env.update(RETAILOPS_OFFICIAL_SERVICE="1", RETAILOPS_CUSTOMER_BUTTON="1",
               RETAILOPS_ALIGNED_TRACE=str(out/"events.json"))
    command = make_command(cfg, "medium", [case], out/"native")
    subprocess.run(command, cwd=UPSTREAM, env=env, check=True)
    sims = read_json(out/"native/zh-retail/results.json")["simulations"]
    if len(sims)!=1 or sims[0]["task_id"].split("_")[0]!=case:
        raise RuntimeError("官方试次缺失或标识不符")
    sim = sims[0]
    rows = []
    for m in sim["messages"]:
        if m.get("usage"):
            meta = (m.get("raw_data") or {}).get("bridge_metadata", {})
            rows.append(dict(usage=m["usage"], model=meta.get("actual_model"), effort=meta.get("reasoning_effort")))
    reward = sim["reward_info"]
    if reward is None:
        raise RuntimeError("官方未返回评分")
    return dict(score=reward["reward"], termination=sim["termination_reason"], seed=sim["seed"], **usage(rows))


def blind_case(spec, shop):
    a,b = shop.orders
    actions = {}
    for letter, order in (("a",a), ("b",b)):
        actions["urgent_"+letter] = dict(tool="manage_urgent_tool", args=dict(platform="jd",shop_id=shop.shop,
                                            user_id=shop.uid,order_id=order))
        actions["invoice_"+letter] = dict(tool="manage_invoice_tool",args=dict(order_id=order, invoice_type="个人发票",
                                           title=spec["title"], phone_number=shop.contact))
    return [(message.format(a=a,b=b,title=spec["title"]), [actions[k] for k in wanted], tuple(words))
            for message,wanted,words in spec["steps"]]


async def service_case(suite, index, out):
    from retailops.api import create_app
    from retailops.domain import Domain
    import httpx
    import service_trials
    import service_holdout
    d = Domain()
    app = create_app(out/"service.db",domain=d,timeout=180)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            if suite == "service":
                name = ("单项保留","追加退货")[index]
                return await service_trials.trial(app, client, "B", 0, name, list(service_trials.fixtures(d))[index], out)
            cases = service_holdout.scenarios(d)
            name = ("双单取消","改退款方式")[index]
            return await service_holdout.evaluate(app, client, name, cases[name], 0, out, mode="B")


def custom(suite, index, out, sealed):
    arm = os.environ["RETAILOPS_REFERENCE"]
    booking = os.environ["RETAILOPS_BOOKING_MODE"]
    if suite in {"service","holdout"}:
        row = asyncio.run(service_case(suite, index, out))
        return dict(score=int(row["success"]))
    if suite in {"blind","experience"}:
        from experience_trials import run
        options = {}
        if suite == "blind":
            verify_seal(sealed)
            spec = read_json(sealed)[index]
            options.update(scenario=lambda shop: blind_case(spec, shop), shop_number=spec["number"])
        row = run((out,index,arm,0), **options)
        return dict(score=int(row["final"] and not row["error"]), first=row["first"], wrong_write=row["wrong_write"],
                    turns=row["turns"], corrections=row["corrections"])
    if suite == "scope":
        from memory_scope_pilot import trial
        trial((out,index,arm), booking_mode=booking)
        row = read_json(next(out.glob("*/结果.json")))
        return dict(score=int(row["final"] and all(s==200 for s in row["http"])), first=row["first"])
    if suite == "items":
        from item_pairs import trial
        asyncio.run(trial(out, index))
        row = read_json(out/"score.json")
        return dict(score=int(row["final"]), first=row["plan"])
    if suite == "wait":
        from booking_wait_comparison import trial
        trial((out,index,booking))
        row = read_json(next(out.glob("*/结果.json")))
        return dict(score=int(row["passed"] and not row["premature_draft"] and not row["http_errors"]),
                    booked=row["booked"], solvable=row["solvable"], premature=row["premature_draft"], turns=row["turns"])
    if suite == "booking":
        from booking_pairs import run
        row = run(out,index)
        trace = read_json(out/str(index)/"记录.json")["rows"]
        early = any(r["body"].get("state",{}).get("phase")=="待确认" for r in trace[:(1 if index==1 else len(trace) if index>=2 else 0)])
        return dict(score=int(row["status"]==200 and row["booked"]==(index<2) and not early),
                    booked=row["booked"], solvable=index<2, premature=early, turns=row["turns"])
    if suite == "consult":
        from consultation_checks import run
        try:
            run(out, True, index)
        except AssertionError:
            # 原检查器先保存观测再断言，业务未过仍保留该试次。
            if not (out/str(index)/"结果.json").exists():
                raise
        row = read_json(out/str(index)/"结果.json")
        expected = ["manage_urgent_tool"] if index in (1,2) else []
        decision = row["actions"]==expected and row["consultation_ok"] is not False
        return dict(score=int(decision and row["http"]==200 and row["before"]==[False,0]
                              and row["after"]==[index in (1,2),0]), first=decision)
    raise ValueError("未知自建题组")


def events(out):
    if (out/"events.json").exists():
        return read_json(out/"events.json")
    rows = []
    for path in out.rglob("*.db"):
        with sqlite3.connect(path) as db:
            rows.extend(json.loads(r[0]) for r in db.execute("SELECT body FROM events ORDER BY id"))
    return rows


def evaluate(suite, case, arm, out, sealed):
    configure(arm)
    start = time.monotonic()
    if suite=="tau":
        result = tau(case,out)
    elif suite=="ecom":
        from ecom_paired import episode
        row = asyncio.run(episode(int(case),0,"text" if arm=="text" else "button",out,record_usage=True))
        calls = [json.loads(s) for s in (out/"usage.jsonl").read_text().splitlines()]
        result = dict(**row, **usage(calls))
    else:
        # 加载原实验的固定上游及其依赖后，沿用其订单快照与判定脚本。
        import experience_fixture
        result = custom(suite,int(case),out,sealed)
    trace = events(out)
    stages = Counter(e["stage"] for e in trace)
    tools = Counter(e["tool"] for e in trace if e.get("tool"))
    if "calls" not in result:
        result.update(usage([e for e in trace if e["stage"]=="模型往返"]))
    result.update(stages=dict(stages), tools=dict(tools), seconds=round(time.monotonic()-start,2))
    return result


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("suite","case","arm"):
        parser.add_argument(arg)
    parser.add_argument("folder",type=Path)
    parser.add_argument("--sealed",type=Path)
    args=parser.parse_args()
    result=evaluate(args.suite,args.case,args.arm,args.folder.resolve(),args.sealed)
    write_json(args.folder/"measurement.json",result)
