"""检查客服可读范围及发布后正文完整性。"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from retailops.task_wiki import TaskWiki


class TaskWikiTest(unittest.TestCase):
    def test_catalog_and_frozen_page(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            body = "先区分询问对象，再查询本次账本。"
            page = root / "guide.md"
            page.write_bytes(body.encode())
            meta = dict(适用="切换订单",例外="仅有一个明确对象",
                        sha256=hashlib.sha256(body.encode()).hexdigest())
            (root / "manifest.json").write_text(json.dumps(dict(pages={"guide":meta})))
            wiki = TaskWiki(root)
            self.assertIn("切换订单",wiki.catalog)
            self.assertNotIn(body,wiki.catalog)
            self.assertIn(body,wiki.read("guide"))
            for name in ("note1","../guide",""):
                with self.assertRaises(ValueError):
                    wiki.read(name)
            page.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                wiki.read("guide")
