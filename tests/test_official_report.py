"""核对报告不能丢弃零分、接受重复位置或破坏记忆来源链。"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from common import write_json
from official_memory_report import collect,audit_chain
from retailops.continual_memory import digest
from retailops.official_memory import snapshot,TRANSCRIPTS

class OfficialReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.cfg=dict(提交="冻结",正式=[4],官方示例=dict(提交="官方固定"))
        initial=self.root/"初始空库";initial.mkdir()
        (initial/"_index.md").write_text("# 经验索引\n")
        before=snapshot(initial)["digest"]
        self.paths={}
        for r in (1,2,3):
            for arm in ("关闭","持续"):
                folder=self.root/"正式"/str(r)/arm/"4"/"唯一";folder.mkdir(parents=True)
                row=dict(轮次=r,题号=4,条件=arm,提交="冻结",模型强度匹配=True,核验完成=True,
                         score=0,action=0,search=1,output=1,记忆摘要=before,记忆读取次数=0)
                write_json(folder/"官方评分.json",row);self.paths[(r,4,arm)]=folder
            folder=self.root/"整理"/str(r)/"4"/"唯一";folder.mkdir(parents=True)
            shutil.copytree(initial,folder/"store")
            source=digest(json.loads((self.paths[(r,4,"持续")]/"官方评分.json").read_text()))
            (folder/"transcripts").mkdir()
            (folder/"transcripts"/(source+".txt")).write_text("SESSION 合成完整输入\n")
            write_json(folder/"工具核验.json",[dict(角色="分析",未读回=[],工具=[dict(tool="read",path=TRANSCRIPTS+"/"+source+".txt",offset=0,lines=1)])])
            write_json(folder/"完成.json",dict(输入摘要=before,输出摘要=before,来源摘要=source,来源提交="冻结",官方上游="官方固定",
                文件摘要=snapshot(initial)["files"],流程=dict(inputs_processed=1,inputs_expected=1,failures=[],organized=True,changes=[]),用时秒=1))
        write_json(self.root/"完成.json",dict(最终记忆摘要=before))

    def test_zero_preserved_and_chain_verified(self):
        rows,paths=collect(self.root,self.cfg)
        self.assertEqual(len(rows),6)
        self.assertEqual(sum(x["score"] for x in rows),0)
        self.assertEqual(len(audit_chain(self.root,self.cfg,paths)),3)

    def test_duplicate_rejected(self):
        folder=self.root/"正式/1/关闭/4/重复";folder.mkdir()
        shutil.copyfile(self.paths[(1,4,"关闭")]/"官方评分.json",folder/"官方评分.json")
        with self.assertRaises(ValueError):collect(self.root,self.cfg)

    def test_missing_position_rejected(self):
        (self.paths[(2,4,"持续")]/"官方评分.json").unlink()
        with self.assertRaises(ValueError):collect(self.root,self.cfg)

    def test_modified_snapshot_rejected(self):
        (self.root/"整理/2/4/唯一/store/_index.md").write_text("# 内容被修改\n")
        with self.assertRaises(ValueError):audit_chain(self.root,self.cfg,self.paths)
