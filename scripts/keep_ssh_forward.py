"""维护已有SSH转发；断线后仅清理本程序记录并核对身份的服务器连接，再重连。"""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time


def identity_value(value):
    if not isinstance(value, dict) or set(value) != {"pid", "start"}:
        raise ValueError("连接身份字段不符")
    if type(value["pid"]) is not int or value["pid"] <= 1:
        raise ValueError("连接进程号不合法")
    if not isinstance(value["start"], str) or not value["start"].isdigit():
        raise ValueError("连接启动标识不合法")
    return value


def connection_identity(pid, proc=Path("/proc")):
    folder = proc / str(pid)
    # 只允许当前账户的SSH连接进程，避免清理其他服务或复用后的进程号。
    if folder.stat().st_uid != os.getuid():
        raise RuntimeError("连接不属于当前账户")
    command = (folder / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
    if not command.startswith("sshd: ") or "@notty" not in command:
        raise RuntimeError("目标不是本账户的非交互SSH连接")
    fields = (folder / "stat").read_text().rsplit(")", 1)[1].split()
    return identity_value({"pid": pid, "start": fields[19]})


def release_connection(value, proc=Path("/proc"), terminate=None):
    value = identity_value(value)
    descriptor = None
    try:
        # 先持有进程句柄，防止核对后进程号被复用而误发终止信号。
        if terminate is None:
            descriptor = os.pidfd_open(value["pid"])
        actual = connection_identity(value["pid"], proc)
        if actual != value:
            raise RuntimeError("连接身份已经变化，拒绝清理")
        if terminate is None:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        else:
            terminate(value["pid"], signal.SIGTERM)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def remote_command(mode, value=None):
    source = base64.b64encode(Path(__file__).read_bytes()).decode()
    command = ["python3", "-u", "-c", "import base64;exec(base64.b64decode(" + repr(source) + "))",
               "--remote", mode]
    if value is not None:
        command += ["--identity", json.dumps(identity_value(value), separators=(",", ":"))]
    return "exec " + shlex.join(command)


def ssh_command(alias, remote, forward):
    if not alias or alias.startswith("-") or any(c.isspace() for c in alias):
        raise ValueError("须提供已有SSH配置中的单个主机别名")
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes"]
    if not forward:
        args += ["-o", "ClearAllForwardings=yes"]
    return args + [alias, remote]


def write_state(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
    temp.replace(path)


def log(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def hold_lock(path):
    handle = path.open("a+b")
    handle.seek(0)
    if not handle.read(1):
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        from fcntl import flock, LOCK_EX, LOCK_NB
        flock(handle, LOCK_EX | LOCK_NB)
    return handle


def supervise(alias, state, stop, check=False):
    state.parent.mkdir(parents=True, exist_ok=True)
    lock = hold_lock(state.with_suffix(state.suffix + ".lock"))
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        while not stop.exists():
            old = json.loads(state.read_text(encoding="utf8")) if state.exists() else {}
            identity = old.get("连接")
            if identity:
                try:
                    result = subprocess.run(ssh_command(alias, remote_command("release", identity), False),
                        capture_output=True, text=True, timeout=25, creationflags=creation)
                    if result.returncode:
                        raise RuntimeError("已记录连接尚未安全释放")
                    write_state(state, {"状态": "旧连接已释放", "时间": time.time()})
                except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
                    log({"状态": "等待安全清理", "类型": type(exc).__name__})
                    time.sleep(10)
                    continue
            proc = subprocess.Popen(ssh_command(alias, remote_command("heartbeat"), not check),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf8",
                creationflags=creation)
            known = None
            try:
                for line in proc.stdout:
                    row = json.loads(line)
                    if "连接" in row:
                        known = identity_value(row["连接"])
                        write_state(state, {"状态": "连接正常", "连接": known, "时间": time.time()})
                        log({"状态": "连接正常", "仅检查": check})
                    if stop.exists() or check:
                        break
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            log({"状态": "连接已退出", "返回码": proc.returncode, "已取得身份": known is not None,
                 "诊断": proc.stderr.read()[-2000:]})
            # 已断开的旧服务端连接仍可能占用转发端口，须核对身份后再释放。
            if check and known:
                result = subprocess.run(ssh_command(alias, remote_command("release", known), False),
                    capture_output=True, text=True, timeout=25, creationflags=creation)
                if result.returncode:
                    raise RuntimeError("检查连接的安全清理失败")
                write_state(state, {"状态": "连接与安全清理检查通过", "时间": time.time()})
                return
            if not stop.exists():
                time.sleep(10)
    finally:
        # 正常停止时也清理自己的连接；无法连接服务器时保留身份以供安全恢复。
        old = json.loads(state.read_text(encoding="utf8")) if state.exists() else {}
        if old.get("连接"):
            try:
                result = subprocess.run(ssh_command(alias, remote_command("release", old["连接"]), False),
                    capture_output=True, text=True, timeout=25, creationflags=creation)
                if result.returncode == 0:
                    write_state(state, {"状态": "已停止并释放连接", "时间": time.time()})
            except (subprocess.SubprocessError, OSError):
                log({"状态": "停止时未清理，已保留连接身份"})
        lock.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--remote", choices=("heartbeat", "release"))
    parser.add_argument("--identity")
    args = parser.parse_args()
    if args.remote == "heartbeat":
        log({"连接": connection_identity(os.getppid())})
        while True:
            time.sleep(15)
            log({"存活": True})
    elif args.remote == "release":
        release_connection(json.loads(args.identity))
    else:
        if not args.alias or not args.state or not args.stop_file:
            parser.error("需要SSH别名、状态文件和停止标记路径")
        supervise(args.alias, args.state.resolve(), args.stop_file.resolve(), args.check)


if __name__ == "__main__":
    main()
