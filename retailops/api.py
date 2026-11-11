"""中文零售业务服务。会话令牌通过 Authorization 传递。"""
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .agent import Agent
from .domain import Issue
from .store import Store, dump


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Input):
    email: str = Field(min_length=3, max_length=254)


class Action(Input):
    tool: str = Field(min_length=1, max_length=80)
    args: dict


class Plan(Input):
    revision: int = Field(ge=0)
    actions: list[Action] = Field(min_length=1, max_length=12)


class Version(Input):
    version: int = Field(gt=0)


class Execution(Version):
    request_id: str = Field(min_length=1, max_length=100)


class Turn(Input):
    message: str = Field(min_length=1, max_length=12000)
    mode: Literal["A", "B", "C"] = "B"
    revision: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=100)


class State(BaseModel):
    user_id: str
    revision: int
    version: int
    phase: str
    confirmed_version: int | None
    actions: list[Action]


class Answer(BaseModel):
    reply: str
    state: State
    sources: list[str]


class Receipt(BaseModel):
    allowed: bool
    version: int
    request_id: str
    results: list[dict]
    replayed: bool


class Decision(BaseModel):
    version: int
    allowed: bool
    code: str
    reason: str
    next_step: str


def create_app(path=None, transport=None, capacity=3, waiting=6, timeout=120):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(path or os.getenv("RETAILOPS_DB", "runs/service/retail.sqlite3"))
        async with httpx.AsyncClient(base_url=os.getenv("RETAILOPS_BRIDGE_URL", "http://127.0.0.1:18863/v1/"),
                                     headers={"Authorization": "Bearer " + os.getenv("CODEX_BRIDGE_TOKEN", ""),
                                              "X-Cancel-On-Disconnect": "1"},
                                     timeout=timeout, transport=transport, trust_env=False) as client:
            app.state.agent = Agent(app.state.store, client, capacity, waiting, timeout)
            yield

    app = FastAPI(title="零售业务 Agent", lifespan=lifespan)

    @app.exception_handler(Issue)
    async def issue_handler(request, error):
        sid = getattr(request.state, "sid", None)
        if sid:
            await asyncio.to_thread(app.state.store.record, sid, "请求被拦截", code=error.data["code"])
        return JSONResponse(error.data, status_code=error.status)

    def session(request: Request, authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer "):
            raise Issue("session", "缺少会话令牌", "先核验注册邮箱", 401)
        sid = Store.identifier(authorization[7:])
        with app.state.store.connection() as db:
            app.state.store.row(db, sid)
        request.state.sid = sid
        return sid

    @app.get("/health")
    def health():
        with app.state.store.connection() as db:
            db.execute("SELECT 1 FROM sessions LIMIT 1")
        return {"status": "ready"}

    @app.post("/sessions")
    def login(body: Login) -> dict[str, str]:
        return {"token": app.state.store.open(body.email)}

    @app.get("/state", response_model=State)
    def state(sid=Depends(session)):
        return app.state.store.snapshot(sid)[0]

    @app.post("/plan", response_model=State)
    def plan(body: Plan, sid=Depends(session)):
        return app.state.store.propose(sid, body.revision, [a.model_dump() for a in body.actions])

    @app.post("/confirm", response_model=State)
    def confirm(body: Version, sid=Depends(session)):
        return app.state.store.confirm(sid, body.version)

    @app.post("/execute", response_model=Receipt)
    def execute(body: Execution, sid=Depends(session)):
        return app.state.store.execute(sid, body.version, body.request_id)

    @app.post("/preflight", response_model=Decision)
    def preflight(body: Version, sid=Depends(session)):
        return app.state.store.preflight(sid, body.version)

    @app.post("/messages", response_model=Answer)
    async def message(body: Turn, sid=Depends(session)):
        return await app.state.agent.run(sid, body.message, body.revision, body.mode, body.request_id)

    @app.delete("/messages/{request_id}")
    async def cancel(request_id: str, sid=Depends(session)):
        task = app.state.agent.active.get((sid, request_id))
        if not task:
            raise Issue("inactive", "没有正在处理的请求", "读取当前状态", 404)
        task.cancel()
        return {"status": "已请求取消"}

    @app.get("/events")
    async def events(request: Request, after: int = Query(0, ge=0), follow: bool = True,
                     last_event_id: Annotated[int | None, Header(ge=0)] = None, sid=Depends(session)):
        async def stream():
            cursor = max(after, last_event_id or 0)
            while True:
                rows = await asyncio.to_thread(app.state.store.events, sid, cursor)
                for row in rows:
                    cursor = row["id"]
                    yield f"id: {cursor}\nevent: progress\ndata: {dump(row)}\n\n"
                if len(rows) == 256:
                    continue
                if not follow or await request.is_disconnected():
                    break
                await asyncio.sleep(0.3)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()

