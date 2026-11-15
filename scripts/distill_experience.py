"""仅向模型传入已核验的开发片段。候选保存在私有目录，审阅后才能发布。"""
import json
import os
import sys
from pathlib import Path
import httpx

root = Path(__file__).resolve().parents[1]
rows = json.loads((root/"configs/experience_sources.json").read_text())
for topic, source, quote in rows:
    q, t, i = source.split("/")
    folder = root/f"runs/ecom-v1/results/{q}-{t}-button"
    assert (folder/"score.json").is_file()
    assert any(quote in json.loads(f.read_text())["session"][int(i)]["content"]
               for f in folder.glob("*/trajectory.json"))
prompt = ("根据同题不同走向与顾客在成功前的纠正，按发票、授权、预约各写一条处理方法。"
          "仅返回JSON数组，每项含topic、action、when、counterexample、sources（片段位置字符串数组）。"
          "每条正文共不超过95个汉字。不保留身份、字段值、授权结论。证据不足标待核实。")
response = httpx.post(os.getenv("RETAILOPS_BRIDGE_URL","http://127.0.0.1:18863/v1/")+"chat/completions",
    headers={"Authorization":"Bearer "+os.environ["CODEX_BRIDGE_TOKEN"]}, timeout=120,trust_env=False,
    json=dict(model="gpt-6.1-sol@medium",messages=[dict(role="system",content=prompt),
              dict(role="user",content=json.dumps(rows,ensure_ascii=False))]))
response.raise_for_status()
out = Path(sys.argv[1])
with out.open("x") as f:
    json.dump(response.json(),f,ensure_ascii=False,indent=2)
