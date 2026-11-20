"""后台守护只恢复可核实的传输异常，不把业务零分或校验失败当作补跑理由。"""
import json,tempfile,unittest
from pathlib import Path
from official_memory_watchdog import busy,recoverable,transport
from fcntl import flock,LOCK_EX

class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/"协调状态.json").write_text(json.dumps({"阶段":"正式","本次标识":"测试"}))
        (self.root/"协调异常-测试.json").write_text("{}")
        (self.root/"进度.json").write_text(json.dumps({"阶段":"对话","轮次":2,"题号":24}))
    def tearDown(self):self.temp.cleanup()
    def test_transient_backend_failure(self):
        (self.root/"正式-协调-测试.log").write_text("httpx.HTTPStatusError: Server error '502 Bad Gateway'")
        self.assertTrue(recoverable(self.root))
    def test_validation_failure_is_not_retried(self):
        (self.root/"正式-协调-测试.log").write_text("ValueError: 冻结提交不符")
        self.assertFalse(recoverable(self.root))
    def test_official_zero_is_not_a_retry_reason(self):
        (self.root/"正式-协调-测试.log").write_text("RuntimeError: 单题异常")
        for arm in ("关闭","持续"):
            folder=self.root/"正式"/"2"/arm/"24"/"首次";folder.mkdir(parents=True)
            (folder/"官方评分.json").write_text('{"score":0}')
        self.assertFalse(recoverable(self.root))
    def test_only_unscored_transport_failure_resumes(self):
        (self.root/"正式-协调-测试.log").write_text("RuntimeError: 单题异常")
        for arm in ("关闭","持续"):
            folder=self.root/"正式"/"2"/arm/"24"/"首次";folder.mkdir(parents=True)
            if arm=="关闭":(folder/"官方评分.json").write_text('{"score":0}')
            else:(folder/"运行.log").write_text("httpx.ReadTimeout")
        self.assertTrue(recoverable(self.root))
    def test_existing_coordinator_lock_is_respected(self):
        self.assertFalse(busy(self.root))
        with (self.root/"协调运行锁").open("a") as f:
            flock(f,LOCK_EX);self.assertTrue(busy(self.root))
    def test_old_error_in_long_log_does_not_trigger_retry(self):
        self.assertFalse(transport("httpx.ReadTimeout\n"+"正常\n"*20+"ValueError: 业务失败"))
if __name__=="__main__":unittest.main()
