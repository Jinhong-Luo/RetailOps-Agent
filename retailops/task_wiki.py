"""按冻结版本向 C 上下文提供目录和受限的主题正文。"""
import hashlib
import json
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


class TaskWiki:
    def __init__(self, directory):
        self.root = Path(directory)
        manifest = json.loads((self.root / "manifest.json").read_bytes())
        self.version = sha((self.root / "manifest.json").read_bytes())
        self.pages = manifest["pages"]
        self.catalog = "已验证经验目录（仅供参考，当前账本、授权和政策优先）：\n" + "\n".join(
            f"{key}：{page['适用']}；例外：{page['例外']}" for key, page in self.pages.items())

    def read(self, key):
        if key not in self.pages or not key.isascii() or not key.isalnum():
            raise ValueError("主题不在冻结目录中")
        data = (self.root / (key + ".md")).read_bytes()
        if sha(data) != self.pages[key]["sha256"]:
            raise ValueError("冻结主题内容已变化")
        return "已验证经验正文：" + data.decode()
