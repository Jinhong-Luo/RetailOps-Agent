"""三层记忆实验的冻结输入、分账台账和独立模型桥。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import time

from common import ROOT, read_json, write_json
import ecom_paired as ecom
import httpx
from retailops.memory_budget import ModelBudget, BudgetStopped
from retailops.task_wiki import sha


BUILD = "gpt-6-astra"
ANSWER = "gpt-6.1-sol"
SHORT_LIMIT = dict(calls=54, tokens=800000, request_tokens=250000, seconds=86400)
FULL_LIMIT = dict(calls=280, tokens=3500000, request_tokens=310000, seconds=86400)
PAGES = ("目录", "直给", "关闭")


def append(path, row):
    with path.open("a", encoding="utf8") as stream:
        print(json.dumps(row, ensure_ascii=False), file=stream, flush=True)


def position(batch, suite, number):
    rows = read_json(batch / "本批进度.json")["位置"]
    found = [r for r in rows if r.get("上下文") == "C" and r.get("跨会话记忆") is False
             and r["题集"] == suite and str(r["题号"]) == str(number) and r["类型"] == "公开正式"]
    if len(found) != 1:
        raise ValueError("历史位置缺失或重复")
    return Path(found[0]["证据"]).parent


@contextmanager
def bridge(run, model, port):
    from common import load_env
    load_env()
    executable = Path.home() / ".nvm/versions/node/v24.21.0/bin/codex"
    env = dict(os.environ, PATH=str(executable.parent) + ":" + os.environ["PATH"],
               RETAILOPS_BRIDGE_MODEL=model, HTTPS_PROXY="http://127.0.0.1:17893",
               HTTP_PROXY="http://127.0.0.1:17893", NO_PROXY="127.0.0.1,localhost")
    url = f"http://127.0.0.1:{port}"
    with (run / (model + ".log")).open("a") as log:
        proc = subprocess.Popen(["python3", str(ROOT/"model_bridge/codex_bridge.py"),
            "--codex", str(executable), "--port", str(port), "--runtime-dir", str(run/"桥运行"),
            "--timeout", "180"], env=env, stdout=log, stderr=log)
        try:
            for _ in range(60):
                if proc.poll() is not None:
                    raise RuntimeError("实验桥启动失败")
                try:
                    with httpx.Client(trust_env=False, timeout=2) as client:
                        if client.get(url+"/health").json()["model"] == model:
                            break
                except httpx.ConnectError:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError("桥未就绪")
            os.environ.update(OPENAI_API_BASE=url+"/v1", OPENAI_API_KEY=os.environ["CODEX_BRIDGE_TOKEN"])
            yield
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)


class PositionLimit(Exception):
    """单个短回放的模型预算已用尽。"""


class FirstDecision(Exception):
    """首个非记忆业务决定已执行，结束单决策回放。"""


class Meter(httpx.AsyncBaseTransport):
    def __init__(self, ledger, folder, model, effort, cap=None, first_decision=False):
        self.ledger, self.folder = ledger, folder
        self.model, self.effort, self.cap = model, effort, cap
        self.target = httpx.AsyncHTTPTransport(retries=0)
        self.count = 0
        self.decided = False
        self.first_decision = first_decision

    async def handle_async_request(self, request):
        payload = json.loads(request.content)
        if payload["model"] != self.model + "@" + self.effort:
            raise RuntimeError("请求档位偏离冻结配置")
        if self.decided:
            raise FirstDecision()
        if self.cap is not None and self.count >= self.cap:
            raise PositionLimit()
        self.count += 1
        name = self.folder.name
        call = self.ledger.reserve(name, payload)
        try:
            response = await self.target.handle_async_request(request)
            await response.aread()
        except BaseException:
            self.ledger.finish(call)
            raise
        self.ledger.finish(call, response)
        if not response.is_success:
            raise BudgetStopped(self.ledger.summary()["停止原因"])
        body = response.json()
        meta = body["bridge_metadata"]
        if (meta["actual_model"], meta["reasoning_effort"]) != (self.model, self.effort):
            raise ValueError("真实模型或档位不符")
        messages = payload["messages"]
        assistant = body["choices"][0]["message"]
        tools = [c["function"]["name"] for c in assistant.get("tool_calls") or []]
        if self.first_decision and tools != ["read_task_wiki"]:
            self.decided = True
        roles = [m.get("content") or "" for m in messages if m["role"] in {"tool","system"}]
        role = "建库" if self.model == BUILD else (
            "客服" if any("你是中文零售助手" in s for s in roles) else "顾客")
        row = dict(序=call,角色=role,模型=self.model,档位=self.effort,用量=body["usage"],
                   目录=any("已验证经验目录" in s for s in roles),
                   正文=any("已验证经验正文：" in s for s in roles),动作=tools or ["回复"])
        append(self.folder/"请求.jsonl", row)
        append(self.folder/"私有回复.jsonl", assistant)
        return response

    async def aclose(self):
        await self.target.aclose()


def client(meter):
    return httpx.AsyncClient(base_url=os.environ["OPENAI_API_BASE"]+"/",trust_env=False,
        headers={"Authorization":"Bearer "+os.environ["OPENAI_API_KEY"]}, transport=meter,timeout=210)


def setup():
    os.umask(0o077)
    os.environ.update(RETAILOPS_REFERENCE="无记忆", RETAILOPS_MODEL=ANSWER+"@medium",
        RETAILOPS_ALIGNED_ARM="full", RETAILOPS_BOOKING_MODE="轻量", RETAILOPS_ECOM_PRODUCT="1",
        RETAILOPS_ECOM_REFUND="1", RETAILOPS_ECOM_BOOKING="1", RETAILOPS_CAPTURE_MEMORY_INPUT="0")
