"""用未进入题库的录制会话验证两种短窗配置实际发给模型的消息。"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx
from retailops.agent import Agent
from retailops.domain import Domain
from retailops.store import Store


async def capture(domain, path, arm):
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "收到。"}}]})

    with patch.dict(os.environ, {"RETAILOPS_ALIGNED_ARM": arm, "RETAILOPS_REFERENCE": "无记忆"}):
        store = Store(path, domain)
        user = next(iter(domain.seed["users"].values()))
        sid = store.identifier(store.open(user["email"]))
        async with httpx.AsyncClient(base_url="http://录制.test/", transport=httpx.MockTransport(reply)) as client:
            agent = Agent(store, client)
            for index, line in enumerate(["#W12324我叫它橘皮书签。", "查昨天的派送", "那上午呢", "今天呢", "已收到",
                                          "我怎么称呼#W12324？"]):
                await agent.run(sid, line, store.snapshot(sid)[0]["revision"], "B", str(index))
    return requests[-1]


async def check():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        domain = Domain()
        on = await capture(domain, root/"开.db", "short_history")
        off = await capture(domain, root/"关.db", "short_no_recall")
        current = lambda r: [m["content"] for m in r["messages"] if m["role"] == "user"]
        assert current(on) == ["#W12324我叫它橘皮书签。", "今天呢", "已收到", "我怎么称呼#W12324？"]
        assert current(off) == current(on)[1:]
        assert on["tools"] == off["tools"]
        assert on["messages"][0] == off["messages"][0]
        print("短窗两组真实客服请求核验通过，关闭时未发送首轮昵称。")
        plain = await capture(domain, root/"全文.db", "policy_text_only")
        search = await capture(domain, root/"章节.db", "policy_with_search")
        assert plain["messages"] == search["messages"]
        prompt = plain["messages"][0]["content"]
        assert domain.policy in prompt and "指出支持结论的原政策段落" in prompt
        tools = lambda r: {tool["function"]["name"] for tool in r["tools"]}
        assert tools(search) == tools(plain) | {"search_policy"}
        assert "search_policy" not in tools(plain)
        print("政策对照的实际请求中全文和引用要求相同，工具差异仅为章节查找。")
        full = await capture(domain, root/"常规.db", "full")
        basic = await capture(domain, root/"基础.db", "basic_context")
        assert current(full) == current(basic) and len(current(full)) == 6
        assert domain.policy in basic["messages"][0]["content"]
        assert tools(full) - tools(basic) == {"search_policy", "find_variants"}
        assert "get_product_details" in tools(basic)
        has_state = lambda r: any(m.get("content", "").startswith("服务端记录的业务进度") for m in r["messages"])
        assert has_state(full) and not has_state(basic)
        print("基础上下文保留完整历史、政策和原始商品工具，移去状态段、政策查找和款式分页。")


if __name__ == "__main__":
    asyncio.run(check())
