"""实验模型请求共用的持久预算。日志只记录用量和失败类别。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

import httpx


class BudgetStopped(RuntimeError):
    """实验停止发出新模型请求。"""


def stop_reason(response):
    """只记录可分类的状态，不把服务商错误正文写入可公开摘要。"""
    status = response.status_code
    if status == 401:
        return "桥接认证失败"
    body = response.text.casefold()
    if status == 429 or any(code in body for code in
                            ("usagelimitexceeded", "usage_limit_reached", "insufficient_quota")):
        return "账户用量或频率额度不足"
    if "workspace routing discovery failed" in body:
        return "上游工作区路由不可用"
    return f"模型请求返回 HTTP {status}，原因待核对"


class ModelBudget:
    def __init__(self, path, limits):
        self.path = Path(path)
        self.limits = dict(limits)
        required = {"calls", "tokens", "seconds", "request_tokens"}
        if set(limits) != required or any(type(v) is not int or v <= 0 for v in limits.values()):
            raise ValueError("预算字段必须为正整数")
        if limits["request_tokens"] > limits["tokens"]:
            raise ValueError("单请求额度超过总预算")
        with self.transaction() as db:
            db.execute("create table if not exists run (id integer primary key, limits text, deadline real, stop text)")
            db.execute("create table if not exists calls (id integer primary key, role text, reserved integer, used integer, status text)")
            config = json.dumps(limits, sort_keys=True)
            db.execute("insert or ignore into run values (1, ?, ?, null)", (config, time.time() + limits["seconds"]))
            if db.execute("select limits from run").fetchone()[0] != config:
                raise ValueError("续跑预算与原配置不一致")

    @contextmanager
    def transaction(self):
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute("begin immediate")
            yield db

    def reserve(self, role, payload):
        # UTF-8 字节数用于保守估计输入，额外预留桥接包装及模型输出。
        planned = len(json.dumps(payload, ensure_ascii=False).encode("utf8")) + 8192
        with self.transaction() as db:
            stop, deadline = db.execute("select stop, deadline from run").fetchone()
            count, charged = db.execute("select count(*), coalesce(sum(coalesce(used, reserved)),0) from calls").fetchone()
            if stop:
                raise BudgetStopped(stop)
            if time.time() >= deadline:
                raise BudgetStopped("实验起始时限已到")
            if count >= self.limits["calls"] or charged + planned > self.limits["tokens"]:
                raise BudgetStopped("实验剩余预算不足")
            if planned > self.limits["request_tokens"]:
                raise BudgetStopped("单请求预留超过上限")
            return db.execute("insert into calls values (null, ?, ?, null, '已发起')", (role, planned)).lastrowid

    def finish(self, call_id, response=None):
        used = None
        stop = None
        status = "传输结果不明"
        if response is not None:
            status = f"HTTP {response.status_code}"
            if response.is_success:
                try:
                    used = response.json()["usage"]["total_tokens"]
                    if type(used) is not int or used < 0:
                        used = None
                except (KeyError, ValueError, TypeError):
                    pass
            if not response.is_success:
                stop = stop_reason(response) + "，保留预留并停止"
            elif used is None:
                stop = "模型未返回可核验用量，保留预留并停止"
        else:
            stop = "模型请求传输结果不明，保留预留并停止"
        with self.transaction() as db:
            reserved = db.execute("select reserved from calls where id=?", (call_id,)).fetchone()[0]
            if used is not None and used > reserved:
                stop = "实际用量超过单请求预留，停止后续请求"
            db.execute("update calls set used=?, status=? where id=?", (used, status, call_id))
            if stop:
                db.execute("update run set stop=coalesce(stop, ?)", (stop,))
        if stop and response is not None and response.is_success:
            raise BudgetStopped(stop)

    def summary(self):
        with self.transaction() as db:
            count, used, pending = db.execute(
                "select count(*), coalesce(sum(used),0), coalesce(sum(case when used is null then reserved else 0 end),0) from calls"
            ).fetchone()
            stop = db.execute("select stop from run").fetchone()[0]
        return dict(请求数=count, 已记录token=used, 未结预留=pending, 停止原因=stop)


class BudgetedAsyncClient(httpx.AsyncClient):
    def __init__(self, budget, role, **kwargs):
        super().__init__(**kwargs)
        self.budget, self.role = budget, role

    async def post(self, url, *, json, **kwargs):
        call_id = self.budget.reserve(self.role, json)
        try:
            response = await super().post(url, json=json, **kwargs)
        except BaseException:
            self.budget.finish(call_id)
            raise
        self.budget.finish(call_id, response)
        return response


class BudgetedClient(httpx.Client):
    def __init__(self, budget, role, **kwargs):
        super().__init__(**kwargs)
        self.budget, self.role = budget, role

    def post(self, url, *, json, **kwargs):
        call_id = self.budget.reserve(self.role, json)
        try:
            response = super().post(url, json=json, **kwargs)
        except BaseException:
            self.budget.finish(call_id)
            raise
        self.budget.finish(call_id, response)
        return response
