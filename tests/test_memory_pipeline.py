"""验证真实客服开关和提炼输入边界。"""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from retailops.agent import Agent
from retailops.continual_memory import ContinualMemory,empty_bank
from memory_dream import private_input

class MemoryPipelineTests(unittest.TestCase):
    def test_agent_switch(self):
        domain=SimpleNamespace(definitions={},reads=set(),writes=set(),policy="")
        store=SimpleNamespace(domain=domain)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"记忆.json"
            path.write_text(json.dumps(empty_bank()),encoding="utf8")
            with patch.dict(os.environ,{"RETAILOPS_REFERENCE":"持续记忆","RETAILOPS_CONTINUAL_BANK":str(path)}):
                self.assertIsInstance(Agent(store,None).experience,ContinualMemory)
            with patch.dict(os.environ,{"RETAILOPS_REFERENCE":"无记忆","RETAILOPS_CONTINUAL_BANK":"不存在"}):
                self.assertEqual(Agent(store,None).experience.retrieve("预约",{"needs":[]}),("",[]))

    def test_no_hidden_task_or_future_script(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            (p/"trajectory.json").write_text(json.dumps(dict(session=[dict(role="user",content="现在只查进展")],
                customer=[dict(role="system",content="未公开顾客计划")],hidden="参考答案")),encoding="utf8")
            (p/"官方评分.json").write_text(json.dumps(dict(score=0,action=0,search=1,output=1,reference="参考答案")),encoding="utf8")
            with sqlite3.connect(p/"business.db") as db:
                db.execute("create table sessions(history text)")
                db.execute("insert into sessions values (?)",(json.dumps([dict(role="system",content="系统材料"),
                    dict(role="assistant",content="尚未办理")]),))
                db.execute("create table accounts(body text)")
                db.execute("insert into accounts values (?)",(json.dumps({"姓名":"李明"}),))
            transcript,sensitive,feedback=private_input(p)
            self.assertNotIn("未公开顾客计划",json.dumps(transcript,ensure_ascii=False))
            self.assertNotIn("参考答案",json.dumps(feedback,ensure_ascii=False))
            self.assertEqual(len(transcript),2)
            self.assertIn("李明",sensitive)
