"""单次实验后台守护：仅恢复明确传输异常，已有官方评分不重采，完成后核验并停止。"""
import argparse,json,os,subprocess,time
from pathlib import Path
from fcntl import flock,LOCK_EX,LOCK_NB
from urllib.request import build_opener,ProxyHandler,Request


def read(p):
    return json.loads(p.read_text())


def write(p,value):
    temp=p.with_suffix(".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")
    temp.replace(p)


def busy(root):
    with (root/"协调运行锁").open("a") as lock:
        try:flock(lock,LOCK_EX|LOCK_NB)
        except BlockingIOError:return True
    return False


def transport(text):
    # 不将模型输出、业务校验或普通HTTP错误当作可重试网络失败。
    last="\n".join(text.splitlines()[-12:])
    return any(s in last for s in (
        "httpx.ReadTimeout", "httpx.ConnectError", "httpx.ConnectTimeout",
        "httpx.RemoteProtocolError", "httpx.HTTPStatusError: Server error '502 Bad Gateway'",
        "httpx.HTTPStatusError: Server error '503 Service Unavailable'"))


def recoverable(root):
    state=read(root/"协调状态.json")
    key=state["本次标识"];phase=state["阶段"]
    marker=root/("协调异常-"+key+".json")
    log=root/(phase+"-协调-"+key+".log")
    if not marker.exists() or not log.exists():return False
    if transport(log.read_text()):return True
    current=read(root/"进度.json")
    if current["阶段"]!="对话":return False
    failures=[]
    for arm in ("关闭","持续"):
        base=root/"正式"/str(current["轮次"])/arm/str(current["题号"])
        if list(base.glob("*/官方评分.json")):continue
        attempts=sorted(base.glob("*/运行.log"),key=lambda p:p.stat().st_mtime)
        if not attempts:return False
        failures.append(transport(attempts[-1].read_text()))
    return bool(failures) and all(failures)


def connected():
    try:
        opener=build_opener(ProxyHandler({"https":"http://127.0.0.1:17893"}))
        with opener.open(Request("https://api.github.com",method="HEAD"),timeout=15) as response:
            return response.status==200
    except Exception:return False


def run(a):
    with (a.root/"后台守护锁").open("a") as lock:
        flock(lock,LOCK_EX|LOCK_NB)
        while not (a.root/"后台停止").exists():
            try:
                if (a.root/"完成.json").exists():
                    subprocess.run([a.python,str(a.reporter),str(a.root),str(a.root/"脱敏结果")],check=True,cwd=a.reporter.parent.parent)
                    write(a.root/"后台状态.json",dict(状态="全部完成并核验",停止=True,时间=time.time()))
                    return
                if busy(a.root):
                    write(a.root/"后台状态.json",dict(状态="实验运行中",时间=time.time()))
                elif not recoverable(a.root):
                    write(a.root/"后台状态.json",dict(状态="需人工检查",原因="未确认是传输异常，拒绝自动重跑",时间=time.time()))
                    return
                elif connected():
                    progress=read(a.root/"进度.json")
                    slot="/".join(str(progress[k]) for k in ("轮次","题号","阶段"))
                    p=a.root/"后台恢复计数.json"
                    counts=read(p) if p.exists() else {}
                    if counts.get(slot,0)>=3:
                        write(a.root/"后台状态.json",dict(状态="需人工检查",原因="同一位置连续恢复已达三次",位置=slot))
                        return
                    actual=subprocess.check_output(["git","-C",str(a.tree),"rev-parse","HEAD"],text=True).strip()
                    if actual!=a.commit:raise ValueError("冻结提交变化")
                    subprocess.run(["git","-C",str(a.tree),"diff","--quiet","HEAD","--"],check=True)
                    counts[slot]=counts.get(slot,0)+1;write(p,counts)
                    with (a.root/"总协调.log").open("a") as log:
                        proc=subprocess.Popen([a.python,"-u",str(a.coordinator)],cwd=a.tree,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    write(a.root/"后台状态.json",dict(状态="已恢复未完成位置",位置=slot,进程=proc.pid,时间=time.time()))
                else:
                    write(a.root/"后台状态.json",dict(状态="等待原网络恢复",时间=time.time()))
            except Exception as exc:
                write(a.root/"后台状态.json",dict(状态="需人工检查",原因=type(exc).__name__,时间=time.time()))
                raise
            time.sleep(60)
        write(a.root/"后台状态.json",dict(状态="守护已停止",说明="不终止已有实验进程",时间=time.time()))


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("root","tree","coordinator","reporter"):p.add_argument("--"+k,type=Path,required=True)
    p.add_argument("--python",required=True);p.add_argument("--commit",required=True)
    run(p.parse_args())
