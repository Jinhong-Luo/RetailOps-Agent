"""创建临时容器和数据卷，重建容器后检查已经提交的业务回执。"""
import json
from pathlib import Path
import subprocess
import time
from urllib import request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def main():
    name = "retailops-check-"+uuid.uuid4().hex[:10]
    volume = docker("volume", "create", name)
    token = None
    def start():
        docker("run","-d","--name",name,"-p","127.0.0.1::8000","-v",volume+":/data","retailops-agent:service-v1")
        url = "http://"+docker("port",name,"8000/tcp")
        for _ in range(90):
            try:
                with request.urlopen(url+"/health",timeout=1) as r:
                    if json.load(r)["status"] == "ready":
                        return url
            except OSError:
                if docker("inspect","-f","{{.State.Running}}",name) == "false":
                    raise RuntimeError("容器退出："+docker("logs",name)[-1000:])
                time.sleep(1)
        raise TimeoutError("容器未就绪")

    def post(route,body):
        headers = {"Content-Type":"application/json"}
        if token:
            headers["Authorization"]="Bearer "+token
        req=request.Request(url+route,data=json.dumps(body).encode(),headers=headers)
        with request.urlopen(req,timeout=10) as r:
            return json.load(r)

    try:
        url=start()
        pack=json.loads((ROOT/"vendor/tau2-bench/data/tau2/multilingual/zh/regional/retail.json").read_bytes())
        order=next(o for o in pack["database"]["orders"].values() if o["status"]=="pending")
        token=post("/sessions",{"email":pack["database"]["users"][order["user_id"]]["email"]})["token"]
        act=dict(tool="cancel_pending_order",args=dict(order_id=order["order_id"],reason="ordered by mistake"))
        v=post("/plan",dict(revision=0,actions=[act]))["version"]
        post("/confirm",dict(version=v))
        body=dict(version=v,request_id="重启恢复")
        first=post("/execute",body)
        assert not first["replayed"] and first["results"][0]["status"] == "cancelled"
        docker("rm","-f",name)
        url=start()
        retry=post("/execute",body)
        assert retry["replayed"] and first["results"] == retry["results"]
        print("容器健康检查、取消订单、数据卷保留、重建后回执重放均通过")
    finally:
        subprocess.run(["docker","rm","-f",name],stdout=subprocess.DEVNULL,check=False)
        docker("volume","rm",volume)


if __name__ == "__main__":
    main()

