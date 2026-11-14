"""在固定的 ECom-Bench 文本题上比较原生客服、文字确认和按钮确认。"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import site
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs/ecom-v1"
UP = RUN / "upstream"
BASE = "7b0b49a36fe5efdccb20f6c0dca2b5681c86d975"
UPSTREAM = "bc5018daaf45eea12330941bce68cebd293cfa85"
sys.path[:0] = [str(UP), str(UP / "agent/servers/offline")]
from envs.story.env import MockStoryEnv
from envs.story.tasks import ALL_TASKS
from utils import RunLogger
import server

site.addsitedir(str(ROOT / ".venv/lib/python3.12/site-packages"))
sys.path.extend(map(str, [ROOT, ROOT / "runtime", ROOT / "vendor/tau2-bench/src"]))
import httpx
from customer_button import ButtonService, CustomerTools
from official_service import OfficialService
from retailops.agent import Agent, schema
from retailops.domain import Domain, Issue
from retailops.store import Store, dump
from tau2.agent.llm_agent import LLMAgent
from tau2.data_model.message import ToolMessage, UserMessage

ARMS = ("old", "text", "button")
MODEL = "gpt-6.1-sol"
READ_ACTIONS = {"查询", "余额查询", "信息查询"}
TOOLS = {t.name: t for t in server.mcp._tool_manager.list_tools() if t.name != "get_image_info_tool"}
SCHEMAS = {n: {"type": "function", "function": {"name": n, "description": t.description,
            "parameters": t.parameters}} for n, t in TOOLS.items()}


def partition():
    images = [i for i,t in enumerate(ALL_TASKS) if re.search(
        r"\.(?:jpg|jpeg|png|webp|gif)|dd-static|img\d+\.360buyimg", t.instruction, re.I)
        or "get_image_info" in str(t.metadata)]
    pilot = [1, 3]
    formal = sorted(set(range(len(ALL_TASKS))) - set(images + pilot))
    assert len(ALL_TASKS) == 53 and len(formal) == 33 and len(images) == 18
    return dict(pilot=pilot, formal=formal, images=images)


class Account(dict):
    def model_dump(self, **kwargs):
        return dict(self)


class Shop:
    valid_action_shape = Domain.valid_action_shape

    def __init__(self, env):
        self.env = env
        self.uid = env.task.user_id
        self.identities = self
        self.reads = {n for n in TOOLS if n.startswith(("get_", "compare_", "transfer_"))}
        mixed = {"manage_order_tool","manage_exchange_tool","manage_ecard_tool","register_cashback_by_review_tool"}
        self.writes = set(TOOLS) - self.reads
        self.reads |= mixed
        self.policy = env.agent_wiki.format(platform=env.task.platform,shop_id=env.task.shop_id,user_id=self.uid)
        self.policy += "\n每次模型回复最多调用一个工具。需要多次查询时，先等待上一次查询结果。"
        self.definitions = {n:SimpleNamespace(openai_schema=s,params=TOOLS[n].fn_metadata.arg_model)
                            for n,s in SCHEMAS.items()}

    def find_user_id_by_email(self, value):
        if value != self.uid:
            raise ValueError("上游注入的身份不匹配")
        return self.uid

    def initial(self, uid):
        return self.env._load_data(self.env.data_dir)

    def toolkit(self, data):
        return SimpleNamespace(db=Account(data))

    def invoke(self, data, name, args):
        tool = TOOLS[name]
        kwargs = tool.fn_metadata.arg_model.model_validate(args)
        server.data = data
        # 上游每次调用都整库写盘。各试次在独立进程内运行，评分前统一落盘。
        return tool.fn(**{key:getattr(kwargs,key) for key in kwargs.__class__.model_fields})

    def trace(self, name, args):
        self.env.tool_calls.append({"function":{"name":name,"arguments":dump(args)}})

    def call(self, toolkit, uid, name, args, allowed):
        is_read = name in self.reads and (name not in self.writes or args.get("action") in READ_ACTIONS)
        if name not in allowed or is_read != (allowed is self.reads):
            raise Issue("operation", "此操作需按读写类型处理", "查询直接调用，写入先拟定方案")
        if args.get("user_id",uid) != uid or args.get("shop_id", self.env.task.shop_id) != self.env.task.shop_id:
            raise Issue("owner","顾客或店铺不匹配","核对会话身份")
        if args.get("platform", self.env.task.platform) != self.env.task.platform:
            raise Issue("owner","电商平台不匹配","核对会话平台")
        if args.get("order_id") and args["order_id"] not in toolkit.db.get("orders",{}).get(
                self.env.task.platform,{}).get(self.env.task.shop_id,{}).get(uid,{}):
            raise Issue("owner","订单不属于当前顾客或不存在","核对订单编号")
        try:
            result = self.invoke(toolkit.db,name,args)
        except (ValueError, KeyError, TypeError) as exc:
            raise Issue("missing_or_invalid",str(exc),"核对工具字段") from exc
        if is_read:
            self.trace(name,args)
        return result

    def search(self, query):
        return [{"source":"ECom-Bench","text":self.policy}]


async def episode(task_id, trial, arm, folder):
    env = MockStoryEnv(task_index=task_id,console_verbose=RunLogger(False))
    env._create_isolated_data()
    server.cache_dir = env.cache_dir
    original_save = server.set_data
    server.set_data = lambda data: None
    shop = Shop(env)
    data = shop.initial(shop.uid)
    store = Store(folder / "business.db",shop)
    sid = store.identifier(store.open(shop.uid))
    service = SimpleNamespace(sid=sid,store=store,version=None)
    button = CustomerTools()
    button.confirm = lambda version: ButtonService.click(service,version)
    customer = [{"role":"system","content":env.user_wiki.format(instruction=env.task.instruction)}]
    url = os.environ["OPENAI_API_BASE"].rstrip("/")+"/"
    headers = {"Authorization":"Bearer "+os.environ["OPENAI_API_KEY"]}
    seed = 300+trial
    old = LLMAgent(list(shop.definitions.values()),shop.policy,"openai/"+MODEL,
                   {"api_base":url,"api_key":os.environ["OPENAI_API_KEY"],"seed":seed,
                    "extra_body":{"reasoning_effort":"medium"},"num_retries":0})
    state = old.get_init_state()
    click_schema = schema("click_confirm","顾客核对当前方案并同意时点击此版本，需要修改时发文字。",
                          {"version":{"type":"integer"}},["version"])
    class PairedClient(httpx.AsyncClient):
        async def post(self, path, *, json):
            return await super().post(path,json={**json,"seed":seed})

    async with PairedClient(base_url=url,headers=headers,timeout=300,trust_env=False) as client:
        bot = Agent(store,client,timeout=600)
        bot.schemas = [s for s in bot.schemas if s["function"]["name"]!="find_variants"]
        reply = "亲，需要什么帮助吗？"
        previous = None
        env.session.append(dict(role="assistant",content=reply))
        clicks = 0
        termination = "轮数上限"
        for turn in range(env.max_turn):
            customer.append(dict(role="user",content=reply))
            user_args = dict(model=MODEL+"@medium",messages=customer,seed=seed)
            if arm=="button" and service.version is not None:
                user_args["tools"]=[click_schema]
            response = await client.post("chat/completions",json=user_args)
            response.raise_for_status()
            msg=response.json()["choices"][0]["message"]
            customer.append(msg)
            calls=msg.get("tool_calls") or []
            if calls:
                call=calls[0]
                if arm!="button" or len(calls)!=1 or call["function"]["name"]!="click_confirm":
                    raise ValueError("顾客调用了未提供的工具")
                try:
                    text=button.click_confirm(**json.loads(call["function"]["arguments"]))
                    clicks+=1
                except (TypeError, ValueError) as exc:
                    text="确认失败："+str(exc)
                customer.append(dict(role="tool",tool_call_id=call["id"],content=text))
            else:
                text=msg["content"]
            if text==previous:
                text="###STOP###"
            previous=text
            env.session.append(dict(role="user",content=text))
            if env._is_done(text):
                termination = "顾客结束"
                break
            begin=time.monotonic()
            if arm=="old":
                incoming=UserMessage(role="user",content=text)
                for _ in range(8):
                    answer,state=await asyncio.to_thread(old.generate_next_message,incoming,state)
                    if not answer.tool_calls:
                        reply=answer.content
                        break
                    call=answer.tool_calls[0]
                    try:
                        result=shop.invoke(data,call.name,call.arguments)
                    except (ValueError,KeyError,TypeError) as exc:
                        result=str(exc)
                    shop.trace(call.name,call.arguments)
                    incoming=ToolMessage(role="tool",id=call.id,content=str(result))
                else:
                    termination = "单轮工具调用上限"
                    break
            else:
                record,_=store.snapshot(sid)
                variant=ButtonService if arm=="button" else OfficialService
                if variant.is_confirmed(service,record,text) and record["phase"] in {"待确认","已确认"}:
                    store.confirm(sid,service.version)
                    store.execute(sid,service.version,uuid4().hex)
                    for act in record["actions"]:
                        shop.trace(act["tool"],act["args"])
                    reply="已按您确认的方案完成操作。"
                    service.version=None
                else:
                    try:
                        outcome=await bot.run(sid,text,record["revision"],"B",uuid4().hex)
                    except Issue as exc:
                        if exc.data["code"]=="step_limit":
                            termination = "单轮工具调用上限"
                            break
                        raise
                    reply=outcome["reply"]
                    service.version=None
                    if outcome["state"]["phase"]=="待确认":
                        service.version=outcome["state"]["version"]
                        reply+=dump(outcome["state"]["actions"])+variant.confirmation_prompt(service)
            env.elapsed_time.append(time.monotonic()-begin)
            env.session.append(dict(role="assistant",content=reply))
        if arm!="old":
            with store.connection() as db:
                data=store.toolkit(db,shop.uid).db
        original_save(data)
        (folder/"trajectory.json").write_text(dump({"session":env.session,"tools":env.tool_calls,"customer":customer}),encoding="utf8")
        score,action,search,output,_=env.calculate_reward(env.session,env.elapsed_time or [0.001])
        return dict(score=score,action=action,search=search,output=output,clicks=clicks,turns=turn+1,
                    termination=termination)


def run_one(job):
    task,trial,arm=job
    folder=RUN/"results"/f"{task}-{trial}-{arm}"
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/"score.json"
    if path.exists():
        return
    save=server.set_data
    for retry in range(2):
        attempt=folder/uuid4().hex
        attempt.mkdir()
        try:
            result=asyncio.run(episode(task,trial,arm,attempt))
            tmp=attempt/"score.json"
            tmp.write_text(dump(dict(task=task,trial=trial,arm=arm,**result)),encoding="utf8")
            tmp.replace(path)
            return
        except Exception as exc:
            (attempt/"error.txt").write_text(repr(exc),encoding="utf8")
            if retry:
                print("试次缺失",job,type(exc).__name__,flush=True)
        finally:
            server.set_data=save


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase",choices=("freeze","pilot","formal"))
    args=parser.parse_args()
    def revision(p):
        return subprocess.check_output(["git","-C",str(p),"rev-parse","HEAD"],text=True).strip()
    manifest=dict(project=BASE,upstream=UPSTREAM,model=MODEL,effort="medium",trials=3,
                  arms=ARMS,max_turns=20,per_agent_turn=8,**partition())
    assert revision(UP)==UPSTREAM
    path=RUN/"manifest.json"
    if args.phase=="freeze":
        assert revision(ROOT)==BASE
        path.write_text(dump(manifest),encoding="utf8")
    else:
        assert path.read_text(encoding="utf8")==dump(manifest)
        if args.phase=="formal":
            assert all((RUN/"results"/f"{t}-0-{a}"/"score.json").exists() for t in manifest["pilot"] for a in ARMS)
            lock=RUN/"adapter.sha256"
            digest=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            if not lock.exists():
                lock.write_text(digest,encoding="ascii")
            if lock.read_text(encoding="ascii")!=digest:
                raise ValueError("正式运行器已经变动，须使用新的实验目录")
        jobs=[(t,r,ARMS[(k+i+r)%3]) for r in range(1 if args.phase=="pilot" else 3)
              for i,t in enumerate(manifest[args.phase]) for k in range(3)]
        with ProcessPoolExecutor(max_workers=3) as pool:
            for job,_ in zip(jobs,pool.map(run_one,jobs)):
                print("已处理",job,flush=True)
        if any(not (RUN/"results"/f"{t}-{r}-{a}"/"score.json").exists() for t,r,a in jobs):
            sys.exit("存在未取得评分的试次，详见对应目录")
