"""本机独立后台入口：维护现有转发并检查单次实验完成状态，不依赖聊天轮询。"""
import argparse,json,os,shlex,subprocess,sys,time
from pathlib import Path


def remote_status(alias,root):
    code="import json,pathlib; r=pathlib.Path("+repr(str(root))+"); print(json.dumps({n:json.loads((r/n).read_text()) if (r/n).exists() else None for n in ['后台状态.json','进度.json','完成.json']},ensure_ascii=False))"
    result=subprocess.run(["ssh","-o","ClearAllForwardings=yes","-o","BatchMode=yes",
        "-o","ConnectTimeout=10","-o","ServerAliveInterval=10","-o","ServerAliveCountMax=2",
        alias,"python3 -c "+shlex.quote(code)],capture_output=True,text=True,encoding="utf8",timeout=40,
        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    result.check_returncode()
    return json.loads(result.stdout)


def run(a):
    from keep_ssh_forward import hold_lock,write_state
    lock=hold_lock(a.state.with_suffix(".后台锁"))
    proc=None
    try:
        with a.state.with_suffix(".连接日志").open("a",encoding="utf8") as log:
            while not a.stop_file.exists():
                if proc is None or proc.poll() is not None:
                    # 现有转发若仍存活，排他锁会拒绝第二个维护器；不切断当前模型请求。
                    proc=subprocess.Popen([sys.executable,str(a.forward_script),"--alias",a.alias,
                        "--state",str(a.state),"--stop-file",str(a.stop_file)],stdout=log,stderr=log,
                        creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                try:
                    status=remote_status(a.alias,a.root)
                    write_state(a.state.with_suffix(".实验状态.json"),status)
                    if status["完成.json"] and (status["后台状态.json"] or {}).get("状态")=="全部完成并核验":
                        a.stop_file.write_text("实验全部完成，停止本轮连接维护。",encoding="utf8")
                        return
                except (OSError,ValueError,subprocess.SubprocessError) as exc:
                    write_state(a.state.with_suffix(".实验状态.json"),dict(状态="等待连接恢复",类型=type(exc).__name__,时间=time.time()))
                time.sleep(60)
    finally:
        lock.close()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--alias",required=True)
    for name in ("state","stop-file","forward-script"):p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--root",required=True)
    a=p.parse_args()
    a.state=a.state.resolve();a.stop_file=a.stop_file.resolve();a.forward_script=a.forward_script.resolve()
    run(a)
