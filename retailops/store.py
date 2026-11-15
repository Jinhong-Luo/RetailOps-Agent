"""使用 SQLite 同一事务保存业务变更、确认状态、幂等回执和事件。"""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .domain import Domain, Issue


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def elapsed(start):
    return round((time.perf_counter() - start) * 1000, 3)


class Store:
    def __init__(self, path, domain=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.domain = domain or Domain()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(uid TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, uid TEXT NOT NULL,
                seq INTEGER NOT NULL, version INTEGER NOT NULL, phase TEXT NOT NULL,
                plan TEXT NOT NULL, history TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(sid TEXT, version INTEGER, request TEXT, body TEXT,
                PRIMARY KEY(sid,version), UNIQUE(sid,request));
            CREATE TABLE IF NOT EXISTS request_versions(sid TEXT, request TEXT, version INTEGER,
                PRIMARY KEY(sid,request));
            CREATE TABLE IF NOT EXISTS booking_wait(sid TEXT PRIMARY KEY, condition TEXT,
                provider TEXT, evidence TEXT, revision INTEGER);
            CREATE TABLE IF NOT EXISTS needs(sid TEXT, source_revision INTEGER, tool TEXT, evidence TEXT,
                status TEXT, result TEXT, PRIMARY KEY(sid,source_revision,tool));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, sid TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS events_session ON events(sid,id);
            """)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def open(self, email):
        try:
            uid = self.domain.identities.find_user_id_by_email(email)
        except ValueError as exc:
            raise Issue("identity", "未找到对应身份", "核对注册邮箱", 403) from exc
        token = secrets.token_urlsafe(32)
        sid = self.identifier(token)
        with self.connection(True) as db:
            db.execute("INSERT OR IGNORE INTO accounts VALUES (?,?)", (uid, dump(self.domain.initial(uid))))
            db.execute("INSERT INTO sessions VALUES (?,?,0,0,'空闲','[]','[]')", (sid, uid))
        return token

    @staticmethod
    def identifier(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def row(self, db, sid):
        row = db.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
        if row is None:
            raise Issue("session", "会话无效", "重新核验身份", 401)
        return row

    def state(self, db, row):
        needs = [dict(r) for r in db.execute(
            "SELECT source_revision,tool,evidence,status,result FROM needs WHERE sid=? ORDER BY source_revision,tool",
            (row["sid"],))]
        if row["phase"] in {"待确认", "已确认"}:
            planned = {a["tool"] for a in json.loads(row["plan"])}
            for need in needs:
                if need["tool"] in planned and need["status"] in {"待办理", "待补充"}:
                    need["status"] = row["phase"]
        wait = db.execute("SELECT condition,provider,evidence,revision FROM booking_wait WHERE sid=?",
                          (row["sid"],)).fetchone()
        extra = {"booking_wait": dict(wait)} if wait else {}
        return dict(**extra, needs=needs, user_id=row["uid"], revision=row["seq"], version=row["version"], phase=row["phase"],
                    confirmed_version=row["version"] if row["phase"] in {"已确认", "完成"} else None,
                    actions=json.loads(row["plan"]))

    def snapshot(self, sid):
        with self.connection() as db:
            row = self.row(db, sid)
            return self.state(db, row), json.loads(row["history"])

    def event(self, db, sid, stage, **fields):
        body = dict(stage=stage, session_id=sid[:12], time_ns=time.time_ns(), **fields)
        db.execute("INSERT INTO events(sid,body) VALUES (?,?)", (sid, dump(body)))

    def record(self, sid, stage, **fields):
        with self.connection(True) as db:
            self.event(db, sid, stage, **fields)

    def events(self, sid, after):
        with self.connection() as db:
            self.row(db, sid)
            return [dict(id=r["id"], **json.loads(r["body"])) for r in db.execute(
                "SELECT id,body FROM events WHERE sid=? AND id>? ORDER BY id LIMIT 256", (sid, after))]

    @staticmethod
    def check_seq(row, seq):
        if row["seq"] != seq:
            raise Issue("concurrent_change", "会话已有更新", "读取最新方案后重试")

    def begin_turn(self, sid, message, seq, request):
        with self.connection(True) as db:
            row = self.row(db, sid)
            self.check_seq(row, seq)
            history = json.loads(row["history"]) + [dict(role="user", content=message)]
            # 新需求可能改变操作对象，旧版立即失效，保留待办供下一轮修订。
            phase = "修订中" if row["phase"] in {"待确认", "已确认", "修订中"} else row["phase"]
            db.execute("UPDATE sessions SET seq=seq+1, phase=?,history=? WHERE sid=?",
                       (phase, dump(history), sid))
            self.event(db, sid, "接收消息", request_id=request)
            return self.state(db, self.row(db, sid)), history

    def booking_wait(self, sid, seq, condition, provider, evidence):
        with self.connection(True) as db:
            row = self.row(db, sid)
            self.check_seq(row, seq)
            return self._booking_wait(db, sid, seq, condition, provider, evidence, json.loads(row["history"]))

    def _booking_wait(self, db, sid, seq, condition, provider, evidence, history, lightweight=False):
        if not isinstance(provider, str) or provider not in {"顾客", "业务查询", "安装方", "顾客暂缓"} or not all(isinstance(v,str) and v.strip() for v in (condition,evidence)):
            raise Issue("invalid_fields", "等待条件不完整", "说明条件、来源和证据")
        if not any(evidence in (m.get("content") or "") for m in history if m["role"] in {"user","tool"}):
            raise Issue("invalid_fields", "未找到所引证据", "引用本会话内的原话或已有工具回执")
        previous = db.execute("SELECT condition,provider,evidence FROM booking_wait WHERE sid=?", (sid,)).fetchone()
        if lightweight and previous and (condition,provider) == tuple(previous)[:2]:
            return dict(condition=condition, provider=provider, evidence=previous[2], changed=False)
        changed = not previous or tuple(previous) != (condition, provider, evidence)
        db.execute("INSERT OR REPLACE INTO booking_wait VALUES (?,?,?,?,?)",
                   (sid, condition, provider, evidence, seq))
        self.event(db, sid, "预约等待", changed=changed, provider=provider)
        return dict(condition=condition, provider=provider, evidence=evidence, changed=changed)

    def register_needs(self, sid, seq, items):
        with self.connection(True) as db:
            row = self.row(db, sid)
            self.check_seq(row, seq)
            message = json.loads(row["history"])[-1]["content"]
            if not isinstance(items, list) or not 1 <= len(items) <= 12 or any(
                    not isinstance(i, dict) or not all(isinstance(i.get(k), str) for k in ("tool","evidence","status"))
                    for i in items):
                raise Issue("invalid_fields", "事项结构不完整", "提供工具、原话与状态")
            for item in items:
                tool, evidence, status = (item[k] for k in ("tool", "evidence", "status"))
                if (tool not in self.domain.writes or not isinstance(evidence, str) or not evidence
                        or evidence not in message or status not in {"仅询问", "待办理", "待补充", "暂缓"}):
                    raise Issue("invalid_fields", "事项需引用本轮原话并指向现有工具", "核对原话和状态")
                db.execute("""UPDATE needs SET status='已修订' WHERE sid=? AND tool=?
                    AND status IN ('仅询问','待办理','待补充','暂缓') AND source_revision<?""",
                           (sid, tool, seq))
                db.execute("INSERT OR REPLACE INTO needs VALUES (?,?,?,?,?,NULL)",
                           (sid, seq, tool, evidence, status))
            return self.state(db, row)

    def toolkit(self, db, uid):
        body = db.execute("SELECT body FROM accounts WHERE uid=?", (uid,)).fetchone()[0]
        return self.domain.toolkit(json.loads(body))

    def read(self, sid, name, args):
        with self.connection() as db:
            uid = self.row(db, sid)["uid"]
            return self.domain.call(self.toolkit(db, uid), uid, name, args, self.domain.reads)

    def apply(self, db, row, actions):
        if not 1 <= len(actions) <= 12:
            raise Issue("missing_or_invalid", "一次方案需要一至十二项操作", "列出全部待办")
        tools = self.toolkit(db, row["uid"])
        results = []
        for act in actions:
            if not self.domain.valid_action_shape(act):
                raise Issue("missing_or_invalid", "操作结构不完整", "提供工具名称和参数")
            intent = db.execute("SELECT status FROM needs WHERE sid=? AND tool=? ORDER BY source_revision DESC LIMIT 1",
                                (row["sid"], act["tool"])).fetchone()
            if intent and intent["status"] in {"仅询问", "暂缓"}:
                raise Issue("scope", "此事项仅询问或已暂缓", "取得顾客新的办理意图后再拟案")
            results.append(self.domain.call(tools, row["uid"], act["tool"], act["args"], self.domain.writes))
        return tools, results

    def finish(self, sid, seq, actions=None, messages=(), request=None, incomplete=False, booking=None):
        with self.connection(True) as db:
            row = self.row(db, sid)
            self.check_seq(row, seq)
            history = json.loads(row["history"]) + list(messages)
            if booking is not None:
                self._booking_wait(db, sid, seq, booking.get("condition"), booking.get("provider"),
                                   booking.get("evidence"), history[:-2], lightweight=True)
            if incomplete:
                db.execute("UPDATE sessions SET plan=?,version=version+1,phase='待补充' WHERE sid=?", (dump(actions), sid))
                self.event(db, sid, "待补充", version=row["version"]+1, request_id=request)
            elif actions is not None:
                # 先在私有副本上执行整个方案，复用上游库存、支付、状态检查。
                start = time.perf_counter()
                self.apply(db, row, actions)
                self.event(db, sid, "执行前检查", request_id=request, duration_ms=elapsed(start))
                db.execute("UPDATE sessions SET plan=?,version=version+1,phase='待确认' WHERE sid=?", (dump(actions), sid))
                self.event(db, sid, "等待确认", version=row["version"] + 1, request_id=request)
            db.execute("UPDATE sessions SET history=?,seq=seq+1 WHERE sid=?", (dump(history), sid))
            return self.state(db, self.row(db, sid))

    def confirm(self, sid, version):
        with self.connection(True) as db:
            row = self.row(db, sid)
            if version != row["version"] or row["phase"] not in {"待确认", "已确认", "完成"}:
                raise Issue("stale_confirmation", "确认未绑定当前待办版本", "展示当前完整方案并重新确认")
            if row["phase"] == "待确认":
                db.execute("UPDATE sessions SET phase='已确认',seq=seq+1 WHERE sid=?", (sid,))
                self.event(db, sid, "用户确认", version=version)
            return self.state(db, self.row(db, sid))

    def propose(self, sid, seq, actions):
        # 手工改案也先撤销旧确认。即使新参数校验失败，旧方案也不能执行。
        error = None
        with self.connection(True) as db:
            row = self.row(db, sid)
            self.check_seq(row, seq)
            try:
                self.apply(db, row, actions)
                phase = "待确认"
            except Issue as exc:
                phase, error = "待补充", exc
            db.execute("UPDATE sessions SET seq=seq+1,version=version+1,phase=?,plan=? WHERE sid=?",
                       (phase, dump(actions), sid))
            self.event(db, sid, phase, version=row["version"]+1, code=error.data["code"] if error else None)
            state = self.state(db, self.row(db, sid))
        if error:
            raise error
        return state

    def authorized_tools(self, db, row, version):
        if version != row["version"] or row["phase"] != "已确认":
            raise Issue("unconfirmed", "当前方案缺少有效确认", "向用户展示方案及版本并请求确认")
        return self.apply(db, row, json.loads(row["plan"]))

    def preflight(self, sid, version):
        with self.connection() as db:
            db.execute("BEGIN")
            try:
                self.authorized_tools(db, self.row(db, sid), version)
            except Issue as exc:
                return dict(version=version, **exc.data)
        return dict(version=version, allowed=True, code="ready", reason="当前方案已确认且满足业务规则",
                    next_step="携带方案版本和请求编号执行")

    def execute(self, sid, version, request):
        with self.connection(True) as db:
            row = self.row(db, sid)
            prior = db.execute("SELECT * FROM receipts WHERE sid=? AND request=?", (sid, request)).fetchone()
            alias = db.execute("SELECT version FROM request_versions WHERE sid=? AND request=?", (sid, request)).fetchone()
            if any(r and r["version"] != version for r in (prior, alias)):
                raise Issue("key_conflict", "该请求编号已绑定其他方案", "使用新请求编号")
            receipt = db.execute("SELECT body FROM receipts WHERE sid=? AND version=?", (sid, version)).fetchone()
            if receipt:
                db.execute("INSERT OR IGNORE INTO request_versions VALUES (?,?,?)", (sid, request, version))
                self.event(db, sid, "重放回执", version=version, request_id=request)
                return dict(**json.loads(receipt[0]), replayed=True)
            actions = json.loads(row["plan"])
            start = time.perf_counter()
            tools, results = self.authorized_tools(db, row, version)
            self.event(db, sid, "业务工具执行", duration_ms=elapsed(start), version=version,
                       tools=[a["tool"] for a in actions], request_id=request)
            start = time.perf_counter()
            account = tools.db.model_dump(mode="json", include={"orders", "users"})
            items = [dict(**act, result=result) for act, result in zip(actions, results)]
            body = dict(allowed=True, version=version, request_id=request, results=results, items=items)
            if any(item["tool"] == "schedule_service_tool" for item in items):
                db.execute("DELETE FROM booking_wait WHERE sid=?", (sid,))
            for item in items:
                db.execute("""UPDATE needs SET status='已办理',result=? WHERE sid=? AND tool=?
                    AND source_revision=(SELECT max(source_revision) FROM needs WHERE sid=? AND tool=?)
                    AND status IN ('待办理','待补充','待确认')""",
                           (dump(item["result"]), sid, item["tool"], sid, item["tool"]))
            db.execute("UPDATE accounts SET body=? WHERE uid=?", (dump(account), row["uid"]))
            db.execute("INSERT INTO receipts VALUES (?,?,?,?)", (sid, version, request, dump(body)))
            history = json.loads(row["history"])
            history.append(dict(role="assistant", content="第%d版业务回执：" % version + dump(items)))
            db.execute("UPDATE sessions SET phase='完成',seq=seq+1,history=? WHERE sid=?", (dump(history), sid))
            self.event(db, sid, "完成", duration_ms=elapsed(start), version=version, request_id=request)
            return dict(**body, replayed=False)
