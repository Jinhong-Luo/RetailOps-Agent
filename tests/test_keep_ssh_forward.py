"""核验断线清理仅针对本程序已记录且身份未变的SSH连接。"""
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock
from keep_ssh_forward import connection_identity, identity_value, release_connection, ssh_command


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "42"
        self.folder.mkdir()
        (self.folder / "cmdline").write_bytes(b"sshd: tester@notty\0")
        (self.folder / "stat").write_text("42 (sshd) " + " ".join(["S"] + ["0"] * 18 + ["1234"]))
        self.kill = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def test_only_verified_connection_is_released(self):
        self.assertEqual(connection_identity(42, self.root), {"pid": 42, "start": "1234"})
        self.assertTrue(release_connection({"pid": 42, "start": "1234"}, self.root, self.kill))
        self.kill.assert_called_once_with(42, signal.SIGTERM)

    def test_reused_process_number_is_rejected(self):
        with self.assertRaises(RuntimeError):
            release_connection({"pid": 42, "start": "5678"}, self.root, self.kill)
        self.kill.assert_not_called()

    def test_other_service_is_rejected(self):
        (self.folder / "cmdline").write_bytes(b"python business_server.py\0")
        with self.assertRaises(RuntimeError):
            release_connection({"pid": 42, "start": "1234"}, self.root, self.kill)
        self.kill.assert_not_called()

    def test_disappeared_connection_needs_no_signal(self):
        self.assertFalse(release_connection({"pid": 43, "start": "1234"}, self.root, self.kill))
        self.kill.assert_not_called()

    def test_identity_and_alias_reject_injection(self):
        for value in ({"pid": 1, "start": "2"}, {"pid": "42", "start": "2"},
                      {"pid": 42, "start": "2; kill 1"}):
            with self.assertRaises(ValueError):
                identity_value(value)
        for alias in ("-oProxyCommand=x", "x y", ""):
            with self.assertRaises(ValueError):
                ssh_command(alias, "true", True)

    def test_cleanup_connection_cannot_create_forward(self):
        args = ssh_command("existing-host", "true", False)
        self.assertIn("ClearAllForwardings=yes", args)
        self.assertIn("BatchMode=yes", args)
        self.assertEqual(args[-2:], ["existing-host", "true"])


if __name__ == "__main__":
    unittest.main()
