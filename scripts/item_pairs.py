"""按历史提交装载原客服和第 1 轮客服，同一对开发请求分别检查草案和终态。"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("571ec11666ea8d82f64cc4ca885940a5b00ec969","455e724909e1724f8f70f67d1023089132c48c0e")


async def trial(out, index):
    from experience_fixture import ExperimentShop
    from retailops.agent import Agent
    from retailops.store import Store
    import httpx
    d = ExperimentShop(900 + index)
    store = Store(out / "business.db", d)
    sid = store.identifier(store.open(d.uid))
    a,b = d.orders
    message = (f"{a}请催发货；请给{b}开个人发票，开票写青榆，电话用账户资料。"
               if index == 0 else f"请为{a}催发货，至于个人发票我想了解能不能开，这次先不用开。")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18863/v1/",trust_env=False,timeout=180,
        headers={"Authorization":"Bearer "+os.environ["CODEX_BRIDGE_TOKEN"]}) as client:
        bot = Agent(store,client,timeout=180)
        result = await bot.run(sid,message,0,"B","首次请求")
    state=result["state"]
    expected=[dict(tool="manage_urgent_tool",args=dict(platform="jd",shop_id=d.shop,user_id=d.uid,order_id=a))]
    if index==0:
        expected.append(dict(tool="manage_invoice_tool",args=dict(title="青榆",order_id=b,invoice_type="个人发票",phone_number=d.contact)))
    def canonical(actions):
        return sorted(json.dumps(act,sort_keys=True) for act in actions)
    good=state["phase"]=="待确认" and canonical(state["actions"])==canonical(expected)
    if good:
        store.confirm(sid,state["version"])
        store.execute(sid,state["version"],"按钮")
    with store.connection() as db:
        data=store.toolkit(db,d.uid).db
    final=data["orders"]["jd"][d.shop][d.uid][a]["是否加急"] and (
        data["invoices"].get(b,{}).get("发票抬头")=="青榆" if index==0 else not data["invoices"])
    calls=[e for e in store.events(sid,0) if e["stage"]=="模型往返"]
    reported=dict(plan=good,final=final,reply=result["reply"],calls=len(calls),
        input=sum(e["usage"]["prompt_tokens"] for e in calls),output=sum(e["usage"]["completion_tokens"] for e in calls))
    (out/"score.json").write_text(json.dumps(reported,ensure_ascii=False))
    print(out.name,reported,flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out",type=Path)
    parser.add_argument("--index",type=int)
    args=parser.parse_args()
    if args.index is not None:
        asyncio.run(trial(args.out,args.index))
    else:
        for version in VERSIONS:
            with tempfile.TemporaryDirectory() as cache:
                base=Path(cache)
                for name in ("retailops","scripts/ecom_paired.py"):
                    files=subprocess.check_output(["git","ls-tree","-r","--name-only",version,name],text=True).splitlines()
                    for file in files:
                        dest=base/file
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        dest.write_bytes(subprocess.check_output(["git","show",version+":"+file]))
                for name in ("runs","vendor",".venv","runtime"):
                    (base/name).symlink_to(ROOT/name,target_is_directory=True)
                for file in ("item_pairs.py","experience_fixture.py"):
                    (base/"scripts"/file).write_bytes((ROOT/"scripts"/file).read_bytes())
                for i in (0,1):
                    out=(args.out/f"{version[:7]}-{i}").resolve()
                    out.mkdir(parents=True,exist_ok=False)
                    subprocess.run([sys.executable,str(base/"scripts/item_pairs.py"),str(out),"--index",str(i)],
                                   cwd=base,check=True)
