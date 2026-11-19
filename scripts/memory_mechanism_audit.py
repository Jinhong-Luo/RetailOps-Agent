"""只读复核完整三轮记忆机制；不采集新成绩、不输出原始对话或记忆正文。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from common import read_json, write_json
from retailops.continual_memory import ContinualMemory, apply_update, digest, empty_bank, terms


def read_events(folder):
    with sqlite3.connect((folder / "business.db").resolve().as_uri() + "?mode=ro", uri=True) as db:
        events = [json.loads(row[0]) for row in db.execute("select body from events order by id")]
        history = json.loads(db.execute("select history from sessions").fetchone()[0])
    return events, history


def audit(root, output):
    config = read_json(root / "配置.json")
    complete = read_json(root / "完成.json")
    assert complete["正式位置"] == 198
    scored = {}
    for path in root.glob("正式/*/*/*/*/官方评分.json"):
        score = read_json(path)
        key = (score["轮次"], score["题号"], score["条件"])
        assert key not in scored and score["提交"] == config["提交"]
        scored[key] = (score, path.parent)
    assert len(scored) == 198
    updates = sorted([(read_json(p), p.parent) for p in root.glob("整理/*/*/*/更新.json")],
                     key=lambda pair: pair[0]["版本"])
    assert len(updates) == 99
    initial = root / "初始空记忆.json"
    snapshots = {digest(read_json(initial)): initial}
    origins = {digest(s): (s["轮次"], s["题号"]) for s, _ in scored.values() if s["条件"] == "持续"}
    growth = []
    lineages = {}
    for update, folder in updates:
        bank = read_json(folder / "记忆.json")
        snapshots[digest(bank)] = folder / "记忆.json"
        r, task = origins[update["轨迹摘要"]]
        if task == config["正式"][-1]:
            growth.append(dict(轮次=r,条目数=len(bank["entries"]),
                               状态分布=dict(Counter(x["status"] for x in bank["entries"])),
                               含暂缓或缺证词条数=sum(any(w in x["method"] for w in ("暂缓","缺证","暂停"))
                                                  for x in bank["entries"])))
        for change in update["变更"]:
            lineages.setdefault(change["id"], []).append(
                dict(轮次=r,题号=task,版本=update["版本"],动作=change["action"],
                     来源整题分=scored[(r,task,"持续")][0]["score"],证据序号=change["evidence"]))
    final = read_json(updates[-1][1] / "记忆.json")
    final_rows = {x["id"]: x for x in final["entries"]}
    hits = Counter()
    hit_sizes = Counter()
    classes = []
    first_replay = []
    cases = []
    selected = {(3,50,"持续"),(3,50,"关闭"),(2,49,"持续"),(2,49,"关闭"),
                (3,5,"持续"),(1,52,"持续"),(2,50,"持续")}
    for key, (score, folder) in sorted(scored.items()):
        events, history = read_events(folder)
        counts = Counter(e["stage"] for e in events)
        refs = {e["request_id"]: e for e in events if e["stage"] == "跨会话参考"}
        for e in refs.values():
            hits.update(e["hits"])
            hit_sizes[len(e["hits"])] += 1
        classes.append(dict(轮次=key[0],题号=key[1],条件=key[2],整题=score["score"],
                            对话轮数=score["turns"],拟案=counts["等待确认"],
                            用户确认=counts["用户确认"],完成=counts["完成"]))
        path = snapshots[score["记忆摘要"]]
        memory = ContinualMemory(path)
        before = {x["id"]: x for x in memory.bank["entries"]}
        requests = [e["request_id"] for e in events if e["stage"] == "接收消息"]
        users = [m for m in history if m["role"] == "user"]
        assert len(requests) == len(users)
        if key in selected:
            turns = []
            for i, (request, message) in enumerate(zip(requests, users), 1):
                ref = refs.get(request, {})
                tools = [e["tool"] for e in events if e.get("request_id") == request
                         and e["stage"] in {"查询工具", "工具被拦截"}]
                turns.append(dict(请求序号=i,命中编号=ref.get("hits", []),
                                  命中主题=[before[h]["topic"] for h in ref.get("hits",[])],
                                  顾客消息摘要=hashlib.sha256(message["content"].encode()).hexdigest(),
                                  查询或被拦截工具=tools))
            cases.append(dict(轮次=key[0],题号=key[1],条件=key[2],整题分=score["score"],
                              逐请求=turns,原始轨迹摘要=hashlib.sha256((folder/"trajectory.json").read_bytes()).hexdigest()))
        if key == (3,5,"持续"):
            text = users[0]["content"]
            result, found = memory.retrieve(text, {"needs":[]})
            assert found == refs[requests[0]]["hits"]
            q = terms(text)
            ranking = sorted(
                [dict(id=x["id"],主题=x["topic"],
                      词项交集=len(q & terms(x["topic"]+" "+x["when"]+" "+x["method"])),
                      最近修订版本=x["updated"])
                 for x in before.values()],
                key=lambda x: (-x["词项交集"],-x["最近修订版本"],x["id"]))
            first_replay.append(dict(位置=dict(轮次=3,题号=5),首轮真实命中已复现=True,
                                     命中主题=[before[h]["topic"] for h in found],
                                     前五候选=ranking[:5]))
    summary = []
    for r in (1,2,3):
        for arm in ("关闭","持续"):
            group = [x for x in classes if x["轮次"] == r and x["条件"] == arm]
            failures = [x for x in group if x["整题"] == 0]
            no_draft = sum(x["拟案"] == 0 for x in failures)
            draft_no_confirm = sum(x["拟案"] > 0 and x["用户确认"] == 0 for x in failures)
            partial = sum(x["完成"] > 0 for x in failures)
            assert no_draft + draft_no_confirm + partial == len(failures)
            summary.append(dict(轮次=r,条件=arm,失败数=len(failures),未生成可确认草案=no_draft,
                                有草案无确认=draft_no_confirm,发生办理但整题失败=partial,
                                对话轮数合计=sum(x["对话轮数"] for x in group)))
    # 合成反例只调用纯函数，证明存在的序号不等于内容支持；不修改正式记忆。
    fake = dict(changes=[dict(action="add",topic="取消条件",when="申请取消",
                             method="只有签收订单才能取消",status="有轨迹支持",evidence=[0])])
    result, _ = apply_update(empty_bank(),fake,"合成诊断",[
        dict(role="tool",content="本单未签收，可以取消。")])
    assert result["entries"][0]["method"] == "只有签收订单才能取消"
    selected_ids = ["经验-67411f16e6d062ff","经验-5e3f456acbab1155","经验-03bef766d815b71d"]
    report = dict(日期="2026-11-19",冻结提交=config["提交"],唯一评分位置=len(scored),
                  题后整理=len(updates),轮末记忆=growth,每次注入条数分布=dict(hit_sizes),
                  高频主题=[dict(记忆编号=k,主题=final_rows[k]["topic"],注入次数=v)
                            for k,v in hits.most_common(10)],
                  失败阶段=summary,逐位置=classes,选定案例=cases,离线检索回放=first_replay,
                  证据校验合成反例=dict(与证据相反的规则仍被接受=True,修改正式记忆=False),
                  选定记忆沿革=[dict(编号=k,主题=final_rows[k]["topic"],沿革=lineages[k]) for k in selected_ids],
                  限制="只读观察与纯函数检索回放，不含去掉单条经验后的模型对照；事件计数不判定暂缓是否合理。")
    write_json(output,report)
    print(json.dumps({k:report[k] for k in ("唯一评分位置","题后整理","轮末记忆","每次注入条数分布","失败阶段","离线检索回放","证据校验合成反例")},ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root",type=Path)
    parser.add_argument("output",type=Path)
    args = parser.parse_args()
    audit(args.root,args.output)
