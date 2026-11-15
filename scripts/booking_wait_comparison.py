"""固定顾客消息与按钮，比较两种预约记录方式，包括等待两轮后补充资料。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from experience_fixture import ExperimentShop
from fastapi.testclient import TestClient
from retailops.api import create_app


def trial(job):
    folder, index, mode = job
    shop = ExperimentShop(830 + index)
    oid = shop.orders[1]
    cases = [
        ([f"替{oid}安排维修，姓名电话用我的账户。哪一天我还没定，先别提交。","星期几我还没决定。",
          "现在定周二维修，请提交草案。"], "维修", "周二", 2, [1]),
        ([f"想约{oid}上门检查，先转专线查周四几点有空，有了具体时段才可以提交。",
          "之前转接的排班有结果了吗？","我还在等确切时间，现在有反馈吗？"], "检查", "周四", None, [1,2]),
        ([f"{oid}请安排周三安装，用本账户联系人和电话，直接给我草案。","请按刚才给的要求拟案。"],
         "安装", "周三", 0, [1]),
        ([f"{oid}本来想周六安装，但暂时搁置，别提交。","仍然先放着。",
          "可以继续了，约周六安装，联系人与电话按账户信息，给我核对。"], "安装", "周六", 2, [1]),
    ]
    messages, service, day, authorized, repeats = cases[index]
    expected = dict(platform="jd",shop_id=shop.shop,user_id=shop.uid,order_id=oid,
                    service_type=service,service_time=day,user_name=f"测试联系人{shop.number}",phone_number=shop.contact)
    out = folder / f"{index}-{mode}"
    out.mkdir(parents=True,exist_ok=False)
    rows = []
    presented_early = False
    with TestClient(create_app(out/"账本.db",domain=shop,timeout=180,booking_mode=mode)) as client:
        token = client.post("/sessions",json={"email":shop.uid}).json()["token"]
        client.headers["Authorization"]="Bearer "+token
        store=client.app.state.store
        sid=store.identifier(token)
        for turn, message in enumerate(messages):
            previous=client.get("/state").json()
            response=client.post("/messages",json=dict(revision=previous["revision"],message=message,mode="B",request_id=str(turn)))
            body=response.json()
            rows.append(dict(message=message,http=response.status_code,body=body))
            if not response.is_success:
                break
            state=body["state"]
            if state["phase"]=="待确认":
                if authorized is None or turn<authorized:
                    presented_early=True
                elif state["actions"]==[dict(tool="schedule_service_tool",args=expected)]:
                    version=state["version"]
                    client.post("/confirm",json=dict(version=version)).raise_for_status()
                    client.post("/execute",json=dict(version=version,request_id="按钮")).raise_for_status()
                    break
        with store.connection() as db:
            data=store.toolkit(db,shop.uid).db
        records=data.get("service",{}).get("jd",{}).get(shop.shop,{}).get(shop.uid,{})
        booked=records.get(oid)=={"服务类型":service,"店铺ID":shop.shop,"用户ID":shop.uid,"订单ID":oid,"平台":"jd",
                                  "电话号码":shop.contact,"用户姓名":expected["user_name"],"预约日期":day}
        events=store.events(sid,0)
        calls=[e for e in events if e["stage"]=="模型往返"]
        result=dict(index=index,mode=mode,booked=booked,solvable=authorized is not None,
                    passed=booked if authorized is not None else not records and not presented_early,
                    premature_draft=presented_early,turns=len(rows),repeat_turns=sum(i<len(rows) for i in repeats),
                    wait_writes=sum(e["stage"]=="预约等待" for e in events),
                    http_errors=sum(r["http"]!=200 for r in rows),calls=len(calls),
                    input=sum(e["usage"]["prompt_tokens"] for e in calls),output=sum(e["usage"]["completion_tokens"] for e in calls))
        _,history=store.snapshot(sid)
        (out/"轨迹.json").write_text(json.dumps(dict(rows=rows,events=events,history=history),ensure_ascii=False))
        (out/"结果.json").write_text(json.dumps(result,ensure_ascii=False))
        print(result,flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    args=parser.parse_args()
    jobs=[(args.folder,i,m) for i in range(4) for m in (("完整","轻量") if i%2==0 else ("轻量","完整"))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(trial,jobs))
