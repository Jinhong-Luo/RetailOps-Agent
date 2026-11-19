"""验证官方文件适配的权限、分页、写后核验和可恢复索引。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from retailops.official_memory import FileTools, MEMORY, TRANSCRIPTS, MemoryError, sha, snapshot, validate_wiki


class OfficialFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.memory=self.root/"memory"
        self.transcripts=self.root/"inputs"
        self.memory.mkdir()
        self.transcripts.mkdir()
        self.fs=FileTools(self.memory,self.transcripts,writable=True)
        (self.memory/"_index.md").write_text("# 经验索引\n",encoding="utf8")

    def test_path_and_readonly(self):
        (self.transcripts/"one.txt").write_text("只读证据",encoding="utf8")
        for path in (MEMORY+"/../outside.md","/etc/passwd",MEMORY+"/bad\\name"):
            with self.assertRaises(MemoryError):
                self.fs.execute("read",{"path":path})
        with self.assertRaises(MemoryError):
            self.fs.execute("write",{"path":TRANSCRIPTS+"/one.txt","content":"覆盖","expected_sha":sha("只读证据")})
        (self.memory/"link.md").symlink_to(self.transcripts/"one.txt")
        with self.assertRaises(MemoryError):
            self.fs.execute("read",{"path":MEMORY+"/link.md"})
        with self.assertRaises(MemoryError):
            FileTools(self.memory).execute("write",{"path":MEMORY+"/x.md","content":"写入","expected_sha":""})

    def test_complete_read_and_readback(self):
        name=MEMORY+"/long.md"
        text="\n".join("资料"+str(i) for i in range(301))
        (self.memory/"long.md").write_text(text,encoding="utf8")
        r=self.fs.execute("read",{"path":name})
        self.assertEqual(r["next_offset"],250)
        self.assertFalse(self.fs.full_read(name))
        with self.assertRaises(MemoryError):
            self.fs.execute("write",{"path":name,"content":"简述","expected_sha":r["sha256"]})
        self.fs.execute("read",{"path":name,"offset":250})
        self.assertTrue(self.fs.full_read(name))
        self.fs.execute("edit",{"path":name,"old_string":"资料300","new_string":"证据末尾","expected_sha":sha(text)})
        with self.assertRaises(MemoryError):
            self.fs.assert_verified()
        self.fs.execute("read",{"path":name})
        self.fs.execute("read",{"path":name,"offset":250})
        self.fs.assert_verified()

    def test_conflict_and_sensitive(self):
        name=MEMORY+"/_index.md"
        r=self.fs.execute("read",{"path":name})
        (self.memory/"_index.md").write_text("并发新增的知识",encoding="utf8")
        with self.assertRaises(MemoryError):
            self.fs.execute("write",{"path":name,"content":"覆盖","expected_sha":r["sha256"]})
        self.fs.sensitive=("顾客独有字段",)
        with self.assertRaises(MemoryError):
            self.fs.execute("write",{"path":MEMORY+"/x.md","content":"记住顾客独有字段","expected_sha":""})

    def test_index_and_links(self):
        (self.memory/"invoice.md").write_text("---\nname: invoice\ndescription: 个人开票\n---\n确认字段后拟案。",encoding="utf8")
        with self.assertRaises(MemoryError):
            validate_wiki(self.memory)
        (self.memory/"_index.md").write_text("# 索引\n[[invoice]] — 个人发票\n",encoding="utf8")
        self.assertEqual(validate_wiki(self.memory)["index_entries"],1)
        previous=snapshot(self.memory)["digest"]
        (self.memory/"invoice.md").write_text("---\nname: invoice\ndescription: 个人开票\n---\n[[missing]]",encoding="utf8")
        self.assertNotEqual(previous,snapshot(self.memory)["digest"])
        with self.assertRaises(MemoryError):
            validate_wiki(self.memory)

    def test_search_and_retired(self):
        (self.memory/"invoice.md").write_text("---\nname: invoice\ndescription: 个人发票\n---\n当前政策优先。",encoding="utf8")
        (self.memory/"old.md").write_text("MOVED TO [[invoice]]",encoding="utf8")
        (self.memory/"_index.md").write_text("[[invoice]] — 个人发票",encoding="utf8")
        self.assertEqual(validate_wiki(self.memory)["retired"],1)
        self.assertEqual(len(self.fs.execute("glob",{"path":MEMORY,"pattern":"*.md"})),3)
        self.assertEqual(len(self.fs.execute("grep",{"path":MEMORY,"pattern":"当前政策"})),1)

    def test_official_sources(self):
        root=Path(__file__).resolve().parents[1]/"third_party/anthropic_dreaming"
        manifest=json.loads((root/"来源锁定.json").read_text())
        for x in manifest["文件"]:
            p=root/x["文件名"]
            if p.is_file():
                raw=p.read_bytes()
                self.assertEqual(hashlib.sha1(b"blob "+str(len(raw)).encode()+b"\0"+raw).hexdigest(),x["Git对象"])


if __name__=="__main__":
    unittest.main()
