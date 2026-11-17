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


if __name__ == "__main__":
    asyncio.run(check())
