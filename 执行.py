"""在冻结副本中每题每版本仅执行一次，保留失败和逐题原始证据。"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from common import read_json, write_json, load_env


def custom(case, folder):
    from experience_fixture import ExperimentShop
    from fastapi.testclient import TestClient
    from retailops.api import create_app
    cases = read_json(ROOT / "自建题.json")
    index, spec = next((i, c) for i, c in enumerate(cases) if c["id"] == case)
    shop = ExperimentShop(8100 + index)
    a, b = shop.orders
    message = spec["message"].format(a=a, b=b)
    expected = []
    for key in spec["actions"]:
        oid = a if key[1] == "a" else b
        if key[0] == "U":
            expected.append(dict(tool="manage_urgent_tool", args=dict(order_id=oid, shop_id=shop.shop, user_id=shop.uid, platform="jd")))
        else:
            expected.append(dict(tool="manage_invoice_tool", args=dict(order_id=oid, invoice_type="个人发票" if key[0]=="I" else "企业发票", phone_number=shop.contact, title="青禾")))
    app = create_app(folder / "business.db", domain=shop, timeout=240)
    with TestClient(app) as client:
        token = client.post("/sessions", json=dict(email=shop.uid)).json()["token"]
        client.headers["Authorization"] = "Bearer " + token
        store = app.state.store
        if spec["fixture"].startswith("invalid"):
            with store.connection(True) as db:
                data = store.toolkit(db,shop.uid).db
                target = a if spec["fixture"].endswith("a") else b
                data["orders"]["jd"][shop.shop][shop.uid][target] = {}
                db.execute("UPDATE accounts SET body=? WHERE uid=?", (json.dumps(data),shop.uid))
        def snapshot():
            with store.connection() as db:
                return store.toolkit(db,shop.uid).db
        before = snapshot()
        response = client.post("/messages", json=dict(message=message,mode="B",revision=0,request_id="首轮"))
        state = client.get("/state").json()
        actions = state["actions"] if state["phase"]=="待确认" else []
        norm = lambda rows: sorted(json.dumps(r,sort_keys=True,ensure_ascii=False) for r in rows)
        correct = norm(actions)==norm(expected)
        middle = snapshot()
        executed = None
        if response.is_success and expected and correct:
            version = state["version"]
            client.post("/confirm",json=dict(version=version)).raise_for_status()
            executed = client.post("/execute",json=dict(version=version,request_id="本版执行")).json()
        write_json(folder/"快照.json",dict(before=before,unconfirmed=middle,final=snapshot(),executed=executed))
        return dict(http=response.status_code,reply=response.json().get("reply",""),message=message,
                    rubric=spec["rubric"],expected=expected,actions=actions,params_ok=correct,
                    before_ok=before==middle,error=None if response.is_success else response.json())


def one(suite, case, arm, folder):
    load_env()
    os.environ.update(RETAILOPS_ALIGNED_ARM=arm, RETAILOPS_MODEL="gpt-6.1-sol@medium",
                      RETAILOPS_REFERENCE="提炼记忆", RETAILOPS_BOOKING_MODE="轻量",
                      OPENAI_API_BASE="http://127.0.0.1:18863/v1", OPENAI_API_KEY=os.environ["CODEX_BRIDGE_TOKEN"])
    if suite=="custom":
        return custom(case,folder)
    if suite=="ecom":
        from ecom_paired import episode
        return asyncio.run(episode(int(case),0,"old" if arm=="old" else "button",folder))
    from run_baseline import child_env, make_command
    from common import UPSTREAM
    cfg = read_json(ROOT/"configs/customer_button.json")
    if arm=="old":
        cfg["agent"]="llm_agent"
        cfg.pop("user",None)
    cfg.update(num_trials=1,max_retries=0,max_concurrency=1)
    env=child_env(cfg)
    if arm=="full":
        env.update(RETAILOPS_OFFICIAL_SERVICE="1",RETAILOPS_CUSTOMER_BUTTON="1")
    subprocess.run(make_command(cfg,"medium",[case],folder/"native"),cwd=UPSTREAM,env=env,check=True)
    simulations=read_json(folder/"native/zh-retail/results.json")["simulations"]
    if len(simulations)!=1 or simulations[0]["task_id"].split("_")[0]!=case:
        raise ValueError("题号或次数不符")
    sim=simulations[0]
    return dict(score=sim["reward_info"]["reward"],termination=sim["termination_reason"])


def plan():
    suites = [("custom",[c["id"] for c in read_json(ROOT/"自建题.json")],"no_consult"),
              ("tau",read_json(ROOT/"data/splits.json")["all_task_ids"],"old"),
              ("ecom",read_json(ROOT/"reports/ecom-text-plan.json")["formal"],"old")]
    jobs=[]
    for suite, ids, baseline in suites:
        for index, case in enumerate(ids):
            arms = [baseline,"full"] if index%2==0 else ["full",baseline]
            jobs.extend((suite,str(case),arm) for arm in arms)
    assert len(jobs)==len(set(jobs))==214
    return jobs


def launch(job):
    folder=ROOT/"结果"/"-".join(job)
    if (folder/"result.json").exists():
        return
    folder.mkdir(exist_ok=False,parents=True)
    with (folder/"log.txt").open("w") as log:
        subprocess.run([sys.executable,__file__,*job,str(folder)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)


if __name__=="__main__":
    if len(sys.argv)==1:
        jobs=plan()
        write_json(ROOT/"清单.json",dict(jobs=jobs,sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for name in ("*.py","自建题.json","scripts/*.py","runtime/*.py","retailops/*.py","configs/*.json","data/*.json","reports/ecom-text-plan.json")
            for p in sorted(ROOT.glob(name))}))
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(launch,jobs))
    else:
        suite,case,arm,path=sys.argv[1:]
        try:
            result=one(suite,case,arm,Path(path))
        except Exception as exc:
            traceback.print_exc()
            result=dict(error=type(exc).__name__)
        write_json(Path(path)/"result.json",result)
