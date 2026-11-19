"""验证失败前消息无损留存、顾客隐藏提示排除和重复消息顺序不变。"""
import json
from pathlib import Path
import tempfile
import unittest
import httpx
from memory_capture import start,finish,lossless,restore

class CaptureTests(unittest.TestCase):
    def test_roundtrip_and_unanswered_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            messages=[dict(role="system",content="你是中文零售助手。规则"),dict(role="user",content="请查询"),
                      dict(role="tool",content="实际查到的材料")]
            payload=dict(messages=messages,model="固定模型",tools=[],seed=300)
            first=start(root,payload)
            finish(first,httpx.Response(200,json=dict(choices=[dict(message=dict(role="assistant",content="答复"))])))
            start(root,payload)
            path=root/"客服实际调用.jsonl"
            raw=[json.loads(x) for x in path.read_text().splitlines()]
            packed=lossless(path)
            self.assertEqual(restore(packed),raw)
            self.assertEqual(len(packed["消息字典"]),3)
            self.assertEqual(len(packed["事件"]),3)

    def test_customer_prompt_never_captured(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.assertIsNone(start(root,dict(messages=[dict(role="system",content="顾客隐藏任务脚本")])))
            self.assertFalse(list(root.iterdir()))
