"""验证完整分析、整体整理的真实工具顺序及失败隔离。"""
import json
from pathlib import Path
import tempfile
import time
import unittest

from official_dream import agent_loop, run_workflow, validate_plan
from retailops.official_memory import MEMORY, TRANSCRIPTS, MemoryError, sha, snapshot


def final(value):
    return dict(role="assistant",content=json.dumps(value,ensure_ascii=False))


def tool(name,args):
    return dict(role="assistant",content=None,tool_calls=[
        dict(id="测试调用",type="function",function=dict(name=name,arguments=json.dumps(args,ensure_ascii=False)))])


class OfficialDreamTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.mem=self.root/"store";self.mem.mkdir()
        self.inputs=self.root/"inputs";self.inputs.mkdir()
        self.log=self.root/"log";self.log.mkdir()
        (self.mem/"_index.md").write_text("# 索引\n")
        (self.inputs/"one.txt").write_text("当前工具支持个人发票，顾客已确认姓名和电话。")
        self.path=TRANSCRIPTS+"/one.txt"

    def test_plan_exact_coverage(self):
        good=dict(groups=[[dict(input=self.path,task="分析")]],organizer_task="整理")
        validate_plan(good,[self.path])
        for bad in (dict(groups=[],organizer_task="整理"),
                    dict(groups=[good["groups"][0]*2],organizer_task="整理")):
            with self.assertRaises(MemoryError):
                validate_plan(bad,[self.path])

    def test_complete_workflow_and_readback(self):
        note="---\nname: invoice\ndescription: 个人发票\n---\n按当前工具确认抬头和电话，再拟草案，不套用企业前提。\n"
        queues={
            "调度":[tool("start_workflow",dict(groups=[[dict(input=self.path,task="完整分析指定输入")]],organizer_task="整体整理")),
                    None],
            "分析":[tool("read",dict(path=self.path)),
                    tool("glob",dict(path=MEMORY)),tool("read",dict(path=MEMORY+"/_index.md")),
                    tool("write",dict(path=MEMORY+"/invoice.md",content=note,expected_sha="")),
                    tool("read",dict(path=MEMORY+"/invoice.md")),
                    tool("write",dict(path=MEMORY+"/_index.md",content="# 索引\n[[invoice]] — 个人发票",expected_sha=sha("# 索引\n"))),
                    tool("read",dict(path=MEMORY+"/_index.md")),final(dict(summary="已修正适用条件"))],
            "整理":[tool("glob",dict(path=MEMORY)),tool("read",dict(path=MEMORY+"/_index.md")),
                    tool("read",dict(path=MEMORY+"/invoice.md")),final(dict(index_entries=1,changes=[]))]
        }
        roles=[]
        def call(messages,tools,role):
            roles.append(role)
            x=queues[role].pop(0)
            return final(json.loads(messages[-1]["content"])) if x is None else x
        result=run_workflow(call,[self.path],self.mem,self.inputs,self.log)
        self.assertEqual(result["inputs_processed"],1)
        self.assertTrue(result["organized"])
        self.assertLess(max(i for i,x in enumerate(roles) if x=="分析"),
                        min(i for i,x in enumerate(roles) if x=="整理"))
        audit=json.loads((self.log/"工具核验.json").read_text())
        self.assertFalse(any(x["未读回"] for x in audit))

    def test_premature_finish_is_not_accepted(self):
        count=0
        def call(messages,tools,role):
            nonlocal count
            count+=1
            return final(dict(summary="我完成了"))
        def validate(value):
            raise MemoryError("未读轨迹")
        with self.assertRaises(RuntimeError):
            agent_loop(call,"分析","规则","任务",[],lambda n,a:None,validate,
                       self.log/"未完成.json",time.monotonic()+10,max_calls=2)
        self.assertEqual(count,2)
        self.assertTrue((self.log/"未完成.json").exists())

    def test_one_workflow_only(self):
        plan=dict(groups=[[dict(input=self.path,task="分析")]],organizer_task="整理")
        queues={"分析":[tool("read",dict(path=self.path)),final(dict(summary="无需修改"))],
                "整理":[tool("glob",dict(path=MEMORY)),tool("read",dict(path=MEMORY+"/_index.md")),
                        final(dict(index_entries=0,changes=[]))]}
        leads=0
        actual=None
        def call(messages,tools,role):
            nonlocal leads,actual
            if role!="调度":
                return queues[role].pop(0)
            leads+=1
            if leads==1:
                return tool("start_workflow",plan)
            if leads==2:
                actual=json.loads(messages[-1]["content"])
                return tool("start_workflow",plan)
            self.assertIn("只允许",json.loads(messages[-1]["content"])["说明"])
            return final(actual)
        before=snapshot(self.mem)
        result=run_workflow(call,[self.path],self.mem,self.inputs,self.log)
        self.assertTrue(result["organized"])
        self.assertEqual(snapshot(self.mem),before)


if __name__=="__main__":
    unittest.main()
