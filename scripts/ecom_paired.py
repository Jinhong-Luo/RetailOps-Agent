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


def validate_write_result(name, result):
    # 固定上游每种写工具的成功回执，拒绝和未知文本均不能提交数据库。
    patterns = {
        "manage_invoice_tool": r"已成功开具发票",
        "manage_urgent_tool": r"已将.+设置为加急$",
        "schedule_service_tool": r"预约(安装|维修|检查)服务成功$",
        "manage_return_tool": r"退货申请已进行处理",
        "manage_order_tool": r"订单.+?(已增加|已取消|的地址和手机号已修改)[,，]",
        "manage_exchange_tool": r"订单.+?换货成功，",
        "manage_ecard_tool": r"已(购买|退款)",
        "register_cashback_by_review_tool": r".+晒单返现已经进入返现流程（返现成功）$",
    }
    if name not in patterns or not isinstance(result, str) or not re.match(patterns[name], result):
        raise Issue("business_rule", str(result), "核对业务回执后修改方案")


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
        if hasattr(self, "validate_plan"):
            from ecom_refund import upstream_args
            args = upstream_args(name, args)
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
        if not is_read and any(isinstance(v, str) and not v.strip() for v in args.values()):
            raise Issue("missing_or_invalid", "业务字段为空", "向顾客补问缺少的字段")
        try:
            result = self.invoke(toolkit.db,name,args)
        except (ValueError, KeyError, TypeError) as exc:
            raise Issue("missing_or_invalid",str(exc),"核对工具字段") from exc
        if not is_read:
            validate_write_result(name, result)
        if is_read:
            self.trace(name,args)
        return result

    def search(self, query):
        return [{"source":"ECom-Bench","text":self.policy}]


def enable_refund(shop):
    import ecom_refund
    shop.validate_plan = lambda data, db, uid, actions: ecom_refund.validate(shop, data, db, uid, actions)
    shop.describe_plan = ecom_refund.describe
    shop.candidate_policy = ("取消或退货先查询订单详情核实付款。已付款的单渠道订单有实付金额就采用实付额；固定上游只记订单总金额时，全额京东E卡付款以该字段为准，扣除已退流水。退款参数填写 order_id 和 refund_reason（取消、退货或价保）。"
                             "仅在顾客明确申请价保时拟价保差额。顾客拒绝关联退款时暂停整组，另行确认独立事项。")


def exchange_activity(shop, history, store, sid):
    start = max(i for i,m in enumerate(history) if m["role"]=="user")
    text = history[start]["content"]
    clauses = re.split(r"[。？！?！；;]", text)
    question = next((q for q in clauses if re.search("赠品|活动|以旧换新|回收补贴",q)), "")
    if not question:
        return
    old = re.search("原购|原来|原型号|原机|当初",question)
    new = re.search("换后|换入|换来的|换过来的|新型号|换的新",question)
    if bool(old)==bool(new):
        return
    exchanges = {}
    with store.connection() as db:
        for receipt, in db.execute("SELECT body FROM receipts WHERE sid=? ORDER BY version", (sid,)):
            for item in json.loads(receipt)["items"]:
                if item["tool"]=="manage_exchange_tool" and item["args"].get("action")=="换货":
                    exchanges[item["args"]["order_id"]] = item["args"]
    related = [args for oid,args in exchanges.items() if oid in question]
    if len(related)==1:
        exchange = related[0]
    elif not related and len(exchanges)==1:
        exchange = next(iter(exchanges.values()))
    else:
        return
    pid = exchange["original_product_id" if old else "exchange_product_id"]
    for msg in history[start+1:]:
        for call in msg.get("tool_calls") or []:
            fn = call["function"]
            if fn["name"]=="get_gift_info_tool" and json.loads(fn["arguments"]).get("product_id")==pid:
                return
    return "get_gift_info_tool", dict(platform=shop.env.task.platform,shop_id=shop.env.task.shop_id,product_id=pid)


def consultation_read(shop, history):
    start = max(i for i,message in enumerate(history) if message["role"]=="user")
    text = history[start]["content"]
    targeted = []
    if re.search(r"优惠券|满减|折扣|(?:有|查|问|看|享受|了解).{0,10}优惠|优惠(?:活动|政策|力度|多少)", text):
        targeted.append(("get_discount_info_tool",dict(platform=shop.env.task.platform,shop_id=shop.env.task.shop_id)))
    for name,args in targeted:
        if not any(call["function"]["name"]==name and json.loads(call["function"]["arguments"])==args
                   for message in history[start+1:] for call in message.get("tool_calls") or []):
            return name,args


def opening_read(shop, history, store, sid):
    completed = {}
    names = {}
    for message in history:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                names[call["id"]] = call["function"]["name"]
        elif message.get("role") == "tool":
            name = names.get(message.get("tool_call_id"))
            if name in {"get_user_info_tool", "get_user_orders_info_tool", "manage_order_tool"}:
                result = json.loads(message["content"])
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except ValueError:
                        if name == "get_user_orders_info_tool" and "没有找到" in result:
                            result = {"订单ID": []}
                if isinstance(result, dict) and "code" not in result:
                    completed[name] = result
    args = dict(platform=shop.env.task.platform, shop_id=shop.env.task.shop_id, user_id=shop.uid)
    if "get_user_info_tool" not in completed:
        return "get_user_info_tool", {"user_id": shop.uid}
    if "get_user_orders_info_tool" not in completed:
        return "get_user_orders_info_tool", args
    ids = completed["get_user_orders_info_tool"].get("订单ID", [])
    if ids and "订单ID" not in completed.get("manage_order_tool", {}):
        text = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        target = next((oid for oid in ids if oid in text), ids[0])
        return "manage_order_tool", dict(args, order_id=target, action="查询")
    return exchange_activity(shop,history,store,sid) or consultation_read(shop,history)


def enable_product(shop):
    shop.product_consultation = True
    shop.opening_read = lambda history, store, sid: opening_read(shop, history, store, sid)
    shop.candidate_policy = getattr(shop, "candidate_policy", "") + (
        "\n开场先读买家基本资料和订单详情，订单列表仅提供编号。复用本会话已查到的订单明细。"
        "顾客明确给出商品编号时按该编号查当轮所问的资料；匹配后停止翻其他订单。"
        "维修查维修说明、安装查安装说明、辅材查辅材材料。顾客问某型号有什么活动，先用 get_gift_info_tool 核实该型号的赠品和回收补贴；店铺折扣不能替代商品活动。"
        "问原购型号或换入型号时从已成功执行的换货回执取对应编号。多单指代不明先澄清。"
        "纯咨询查到依据后直接给顾客可见的回答；活动资料只能说明查到的条款。")


async def episode(task_id, trial, arm, folder, record_usage=False):
    env = MockStoryEnv(task_index=task_id,console_verbose=RunLogger(False))
    env._create_isolated_data()
    server.cache_dir = env.cache_dir
    original_save = server.set_data
    server.set_data = lambda data: None
    shop = Shop(env)
    if arm != "old" and os.getenv("RETAILOPS_ECOM_REFUND") == "1":
        enable_refund(shop)
    if arm != "old" and os.getenv("RETAILOPS_ECOM_PRODUCT") == "1":
        enable_product(shop)
    if arm != "old" and os.getenv("RETAILOPS_ECOM_BOOKING") == "1":
        shop.scoped_waits = True
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
            response = await super().post(path,json={**json,"seed":seed})
            if record_usage and response.is_success:
                body = response.json()
                meta = body.get("bridge_metadata", {})
                with (folder/"usage.jsonl").open("a", encoding="utf8") as stream:
                    stream.write(dump(dict(usage=body.get("usage"), model=meta.get("actual_model"),
                                           effort=meta.get("reasoning_effort"))) + "\n")
            return response

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
                    receipt = store.execute(sid,service.version,uuid4().hex)
                    for act in record["actions"]:
                        from ecom_refund import upstream_args
                        shop.trace(act["tool"], upstream_args(act["tool"], act["args"]))
                    reply="本次已确认操作的回执：" + dump(receipt["items"])
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
