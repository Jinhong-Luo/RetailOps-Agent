"""确认测试与正式运行加载同一份已入库脚本，防止上传副本遮蔽。"""
from pathlib import Path
import unittest
import official_dream
import official_memory_rounds

class OfficialImportsTests(unittest.TestCase):
    def test_modules_are_tracked_scripts(self):
        root=Path(__file__).resolve().parents[1]
        for module in (official_dream,official_memory_rounds):
            self.assertEqual(Path(module.__file__).resolve(),root/"scripts"/(module.__name__+".py"))
