"""用模拟回复验证：只有 C 开启组能收到目录、主动读取并在下一笔请求收到正文。"""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ecom_paired import Agent
from verified_memory_pilot import seed
import httpx


class WikiEntryCheck(unittest.IsolatedAsyncioTestCase):
    async def test_modes(self):
        with TemporaryDirectory() as temp:
            root=Path(temp)
            body="本单确认前重新核对授权。"
            (root/"guide.md").write_bytes(body.encode())
            entry=dict(适用="多项办理",例外="首次咨询",sha256=hashlib.sha256(body.encode()).hexdigest())
            (root/"manifest.json").write_text(json.dumps(dict(pages=dict(guide=entry))))
            for context,arm in (("C","关闭"),("B","目录"),("C","目录"),("C","直给")):
                with self.subTest(context=context,arm=arm):
                    store,sid,_=seed(root/(context+arm),"个人开票")
                    payloads=[]

                    def respond(request):
                        obj=json.loads(request.content)
                        payloads.append(obj)
                        can_read=any(t["function"]["name"]=="read_task_wiki" for t in obj["tools"])
                        message=dict(role="assistant",content="请核对本单信息。")
                        if can_read:
                            message=dict(role="assistant",content=None,tool_calls=[dict(id="查阅",
                                type="function",function=dict(name="read_task_wiki",arguments='{"page":"guide"}'))])
                        return httpx.Response(200,json=dict(choices=[dict(message=message)]))

                    with patch.dict(os.environ,RETAILOPS_REFERENCE="无记忆",RETAILOPS_WIKI_MODE=arm,
                                    RETAILOPS_WIKI_DIR=str(root),RETAILOPS_WIKI_PAGE="guide"):
                        async with httpx.AsyncClient(transport=httpx.MockTransport(respond),base_url="http://test") as c:
                            await Agent(store,c).run(sid,"请开个人发票",store.snapshot(sid)[0]["revision"],context,"验收")
                    active=context=="C" and arm!="关闭"
                    self.assertEqual(any("已验证经验目录" in (m.get("content") or "")
                        for m in payloads[0]["messages"]),active)
                    self.assertEqual(any(body in (m.get("content") or "") for m in payloads[-1]["messages"]),active)
                    self.assertEqual(len(payloads),2 if active and arm=="目录" else 1)


if __name__=="__main__":
    unittest.main()
