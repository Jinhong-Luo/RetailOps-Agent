"""在四个固定开发脚本上，用真实消息接口检查预约推进与暂停。"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from experience_fixture import ExperimentShop
from fastapi.testclient import TestClient
from retailops.api import create_app


def run(folder, index):
    d = ExperimentShop(70 + index)
    oid = d.orders[0]
    start = f"给{oid}约周五安装，姓名和电话请查本单资料。"
    scripts = [
        [start, "请用刚才说的要求提交预约草案。"],
        [f"为{oid}预约维修，姓名和电话用账户的，我稍后告诉你星期几。", "定周五维修，请提交。"],
        [start + "必须先查到具体几点才能提交，请转专线。", "转接有确定时段了吗？", "有新结果了吗？"],
        [start + "现在先不要办理，我还没定。", "继续暂缓，别提交。"],
    ]
    out = folder / str(index)
    out.mkdir(parents=True,exist_ok=False)
    app = create_app(out/"账本.db", domain=d, timeout=180)
    rows = []
    with TestClient(app) as client:
        token = client.post("/sessions",json={"email":d.uid}).json()["token"]
        client.headers["Authorization"] = "Bearer " + token
        store = app.state.store
        sid = store.identifier(token)
        for turn, message in enumerate(scripts[index]):
            state = client.get("/state").json()
            response = client.post("/messages",json=dict(message=message,mode="B",revision=state["revision"],request_id=str(turn)))
            row = dict(message=message,code=response.status_code,body=response.json())
            rows.append(row)
            if response.status_code != 200:
                break
            state = row["body"]["state"]
            expected = dict(platform="jd",shop_id=d.shop,user_id=d.uid,order_id=oid,
                            service_type="维修" if index==1 else "安装",service_time="周五",
                            user_name=f"测试联系人{d.number}",phone_number=d.contact)
            if index in (0,1) and (index==0 or turn==1) and state["phase"]=="待确认" and state["actions"]==[dict(tool="schedule_service_tool",args=expected)]:
                v = state["version"]
                client.post("/confirm",json=dict(version=v)).raise_for_status()
                client.post("/execute",json=dict(version=v,request_id="办理")).raise_for_status()
                break
        with store.connection() as db:
            data=store.toolkit(db,d.uid).db
        events=store.events(sid,0)
        _,history=store.snapshot(sid)
        calls=[e for e in events if e["stage"]=="模型往返"]
        result=dict(index=index,booked=bool(data.get("service")),turns=len(rows),
                    calls=len(calls),input=sum(e["usage"]["prompt_tokens"] for e in calls),
                    output=sum(e["usage"]["completion_tokens"] for e in calls),status=rows[-1]["code"])
        (out/"记录.json").write_text(json.dumps(dict(result=result,rows=rows,events=events,history=history),ensure_ascii=False))
        print(result,flush=True)
        return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    folder=parser.parse_args().folder
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda index: run(folder,index),range(4)))
