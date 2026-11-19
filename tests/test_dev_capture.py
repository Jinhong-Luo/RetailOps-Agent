"""直接回归开发运行器的实际请求方法，防止计时变量与记录函数重名。"""
import ast
import json
from pathlib import Path
import tempfile
import time
import unittest
import httpx

class DevelopmentCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_client_records_elapsed_and_response(self):
        source=Path(__file__).resolve().parents[1]/"scripts/official_memory_development.py"
        tree=ast.parse(source.read_text())
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=="Client")
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            scope=dict(httpx=httpx,time=time,folder=Path(directory),calls=calls)
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),"exec"),scope)
            async def respond(request):
                return httpx.Response(200,json=dict(choices=[dict(message=dict(role="assistant",content="合成答复"))],
                    usage=dict(total_tokens=7),bridge_metadata=dict(actual_model="gpt-6.1-sol",reasoning_effort="medium")))
            async with scope["Client"](transport=httpx.MockTransport(respond),base_url="http://test/") as client:
                await client.post("chat/completions",json=dict(messages=[dict(role="system",content="你是中文零售助手。")]))
            self.assertEqual(len(calls),1)
            self.assertGreaterEqual(calls[0]["秒"],0)
            self.assertEqual(calls[0]["用量"]["total_tokens"],7)
            rows=[json.loads(x) for x in (Path(directory)/"客服实际调用.jsonl").read_text().splitlines()]
            self.assertEqual([x["事件"] for x in rows],["请求","返回"])
            self.assertEqual(rows[-1]["状态码"],200)
