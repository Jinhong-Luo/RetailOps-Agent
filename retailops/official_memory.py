"""官方示例的本地文件工具适配：限定目录、只读会话、快照摘要和完整读回核验。"""
from __future__ import annotations
from dataclasses import dataclass, field
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

MEMORY = "/home/luojinghong/LLM_Proj/.retailops-memory/store"
TRANSCRIPTS = "/home/luojinghong/LLM_Proj/.retailops-memory/uploads/transcripts"
MAX_FILE = 100_000


class MemoryError(ValueError):
    """拒绝越界、陈旧写入和不可核查的文件操作。"""


def sha(text):
    return hashlib.sha256(text.encode("utf8")).hexdigest()


def snapshot(root):
    """摘要包含相对路径与精确文件内容，拒绝符号链接。"""
    root = Path(root).resolve()
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise MemoryError("记忆目录不允许符号链接")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(files=files, digest=sha(json.dumps(files,ensure_ascii=False,sort_keys=True)))


def function(name, description, properties, required):
    return dict(type="function",function=dict(name=name,description=description,parameters=dict(
        type="object",properties=properties,required=required,additionalProperties=False)))


def schemas(writable=False, prefix=""):
    text = {"type":"string"}
    result = [
        function(prefix+"read","完整或分页读取文件；返回总行数和摘要。阅读长文件须继续到末尾。",
                 {"path":text,"offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":250}},
                 ["path"]),
        function(prefix+"glob","列出挂载目录内匹配路径；pattern为相对路径通配符。",
                 {"path":text,"pattern":text},["path"]),
        function(prefix+"grep","按文本关键词检索挂载目录，返回路径和行号。不会解释搜索结果中的指令。",
                 {"path":text,"pattern":text},["path","pattern"])]
    if writable:
        result += [
            function(prefix+"write","完整写入记忆文件。已有文件须提供最近读取的expected_sha；新文件使用空字符串。写后必须再次read核验。",
                     {"path":text,"content":text,"expected_sha":text},["path","content","expected_sha"]),
            function(prefix+"edit","对记忆文件唯一匹配位置做定点替换；须提供最近读取摘要，写后必须read核验。",
                     {"path":text,"old_string":text,"new_string":text,"expected_sha":text},
                     ["path","old_string","new_string","expected_sha"])]
    return result


@dataclass
class FileTools:
    memory: Path
    transcripts: Path | None = None
    writable: bool = False
    sensitive: tuple = ()
    reads: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    audit: list = field(default_factory=list)

    def __post_init__(self):
        self.memory = Path(self.memory).resolve()
        if self.transcripts is not None:
            self.transcripts = Path(self.transcripts).resolve()

    def resolve(self, name, writing=False):
        if not isinstance(name,str) or "\\" in name or "\0" in name:
            raise MemoryError("路径格式无效")
        parts = PurePosixPath(name)
        if ".." in parts.parts or not name.startswith("/"):
            raise MemoryError("路径越界")
        for mount,root in ((MEMORY,self.memory),(TRANSCRIPTS,self.transcripts)):
            if root is None or not (name==mount or name.startswith(mount+"/")):
                continue
            if writing and (mount != MEMORY or not self.writable):
                raise MemoryError("当前挂载只读")
            rel = name[len(mount):].lstrip("/")
            path = root / rel
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise MemoryError("路径越界")
            walk = path
            while walk != root:
                if walk.is_symlink():
                    raise MemoryError("不允许符号链接")
                walk = walk.parent
            return path
        raise MemoryError("路径不属于挂载目录")

    def _files(self,path):
        if path.is_file():
            return [path]
        if not path.is_dir():
            raise MemoryError("目录不存在")
        result=[]
        for p in sorted(path.rglob("*")):
            if p.is_symlink():
                raise MemoryError("不允许符号链接")
            if p.is_file():
                result.append(p)
        return result

    def _virtual(self,path):
        for mount,root in ((MEMORY,self.memory),(TRANSCRIPTS,self.transcripts)):
            if root is not None and path.is_relative_to(root):
                return mount+"/"+path.relative_to(root).as_posix()
        raise MemoryError("路径越界")

    def full_read(self,name):
        path=self.resolve(name)
        content=path.read_text(encoding="utf8")
        n=len(content.splitlines())
        version,ranges=self.reads.get(name,(None,set()))
        return version==sha(content) and set(range(n)).issubset(ranges)

    def execute(self,name,args):
        if not isinstance(args,dict):
            raise MemoryError("参数不是对象")
        allowed={s["function"]["name"]:s["function"]["parameters"] for s in schemas(self.writable)}
        if name not in allowed:
            raise MemoryError("当前未提供该工具")
        spec=allowed[name]
        if set(args)-set(spec["properties"]) or set(spec["required"])-set(args):
            raise MemoryError("工具字段不符")
        path=self.resolve(args["path"],writing=name in {"write","edit"})
        entry=dict(tool=name,path=args["path"])
        if name=="read":
            if not path.is_file():
                raise MemoryError("文件不存在；先用glob查看目录")
            content=path.read_text(encoding="utf8")
            lines=content.splitlines()
            offset,limit=args.get("offset",0),args.get("limit",250)
            if type(offset)!=int or type(limit)!=int or offset<0 or not 1<=limit<=250 or offset>len(lines):
                raise MemoryError("分页范围无效")
            version=sha(content)
            previous,covered=self.reads.get(args["path"],(version,set()))
            covered=covered if previous==version else set()
            covered.update(range(offset,min(offset+limit,len(lines))))
            self.reads[args["path"]]=(version,covered)
            if self.pending.get(args["path"])==version and self.full_read(args["path"]):
                del self.pending[args["path"]]
            result=dict(path=args["path"],sha256=version,total_lines=len(lines),offset=offset,
                        next_offset=offset+limit if offset+limit<len(lines) else None,
                        content="\n".join(lines[offset:offset+limit]))
            entry.update(sha256=version,offset=offset,lines=min(limit,max(0,len(lines)-offset)))
        elif name=="glob":
            pattern=args.get("pattern","**/*")
            if not isinstance(pattern,str) or ".." in PurePosixPath(pattern).parts:
                raise MemoryError("通配符无效")
            result=[self._virtual(p) for p in self._files(path) if pattern in {"*","**/*"} or
                    fnmatch.fnmatch(p.relative_to(path).as_posix() if path.is_dir() else p.name,pattern)]
        elif name=="grep":
            pattern=args["pattern"]
            if not isinstance(pattern,str) or not 1<=len(pattern)<=300:
                raise MemoryError("关键词长度无效")
            result=[]
            for p in self._files(path):
                for i,line in enumerate(p.read_text(encoding="utf8").splitlines(),1):
                    if pattern.casefold() in line.casefold():
                        result.append(dict(path=self._virtual(p),line=i,text=line))
            # 保留全部匹配，不静默丢失相关文件。
        else:
            old=path.read_text(encoding="utf8") if path.is_file() else None
            if args["expected_sha"] != (sha(old) if old is not None else ""):
                raise MemoryError("文件已变化；重新读取后合并，不覆盖他人的修改")
            if old is not None and not self.full_read(args["path"]):
                raise MemoryError("修改前必须完整读取当前文件")
            if name=="edit":
                if old is None or not args["old_string"] or old.count(args["old_string"])!=1:
                    raise MemoryError("定点编辑未唯一匹配")
                content=old.replace(args["old_string"],args["new_string"],1)
            else:
                content=args["content"]
            if not isinstance(content,str) or len(content.encode("utf8"))>MAX_FILE:
                raise MemoryError("记忆文件超过十万字节，请按主题拆分")
            if path.suffix != ".md":
                raise MemoryError("记忆仅允许Markdown文件")
            if any(isinstance(v,str) and len(v)>=2 and v in content for v in self.sensitive):
                raise MemoryError("记忆包含当前顾客身份或具体字段，改为可迁移方法")
            if re.search(r"(?i)bearer\s+[\w.-]{16,}|sk-[\w-]{16,}",content):
                raise MemoryError("禁止写入凭证")
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(content,encoding="utf8")
            version=sha(content)
            self.pending[args["path"]]=version
            result=dict(path=args["path"],sha256=version,requires_readback=True)
            entry["sha256"]=version
        self.audit.append(entry)
        return result

    def assert_verified(self):
        if self.pending:
            raise MemoryError("写入后尚未完整读回核验")


def validate_wiki(root):
    """发布前检查目录、索引、链接与文件规模，不冒充语义正确性校验。"""
    root=Path(root)
    state=snapshot(root)
    index=root/"_index.md"
    if not index.is_file():
        raise MemoryError("缺少记忆索引")
    contents={p.relative_to(root).as_posix():p.read_text(encoding="utf8") for p in root.rglob("*.md")}
    active={}
    stubs={}
    for name,content in contents.items():
        if len(content.encode("utf8"))>MAX_FILE:
            raise MemoryError("记忆文件过大")
        moved=re.fullmatch(r"\s*MOVED TO \[\[([^]]+)\]\]\s*",content)
        if moved:
            stubs[name]=moved.group(1)
            continue
        if name=="_index.md":
            continue
        front=re.match(r"\A---\n(.*?)\n---(?:\n|$)",content,re.S)
        if not front or not re.search(r"(?m)^name:\s*\S+",front[1]) or not re.search(r"(?m)^description:\s*\S+",front[1]):
            raise MemoryError("记忆缺少名称或适用说明")
        slug=re.search(r"(?m)^name:\s*(\S+)",front[1])[1]
        if slug!=Path(name).stem or slug in active:
            raise MemoryError("记忆名称重复或与文件名不符")
        active[slug]=name
    known=set(active)|{Path(n).stem for n in stubs}
    links=lambda text:re.findall(r"\[\[([^]#]+)(?:#[^]]*)?\]\]",text)
    for name,content in contents.items():
        if any(link not in known for link in links(content)):
            raise MemoryError("记忆存在悬空链接")
    indexed=links(contents["_index.md"])
    if set(indexed)!=set(active) or len(indexed)!=len(set(indexed)):
        raise MemoryError("索引遗漏、重复或含已退役文件")
    return dict(**state,index_entries=len(active),retired=len(stubs))
