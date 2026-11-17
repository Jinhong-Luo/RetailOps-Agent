"""在独立副本重放原官方评分输入，同时保存原分和复核分。"""
import json
from pathlib import Path
import sqlite3


def ecom(folder, task):
    from ecom_paired import MockStoryEnv, RunLogger, server
    env = MockStoryEnv(task_index=task, console_verbose=RunLogger(False))
    env._create_isolated_data()
    server.cache_dir = env.cache_dir
    with sqlite3.connect(f"file:{folder/'business.db'}?mode=ro", uri=True) as db:
        account=json.loads(db.execute("SELECT body FROM accounts WHERE uid=?",(env.task.user_id,)).fetchone()[0])
    trace=json.loads((folder/"trajectory.json").read_bytes())
    env.tool_calls=trace["tools"]
    env.session=trace["session"]
    server.set_data(account)
    return list(env.calculate_reward(env.session, [.001])[:4])


def tau(folder, environment_only):
    from tau2.data_model.simulation import Results
    from tau2.evaluator.evaluator import EvaluationType
    from tau2.scripts.evaluate_trajectories import compute_simulation_rewards
    path=folder/"native/zh-retail/results.json"
    original=Results.model_validate_json(path.read_bytes())
    assert len(original.simulations)==1
    new=compute_simulation_rewards(original, EvaluationType.ENV if environment_only else EvaluationType.ALL)
    saved=original.simulations[0].reward_info
    replayed=new.simulations[0].reward_info
    assert saved is not None and replayed is not None
    return dict(原整题分=saved.reward,复核分=replayed.reward,原分项=saved.reward_breakdown,
                复核分项=replayed.reward_breakdown,模式="仅数据库" if environment_only else "完整官方复评")


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite",choices=["ecom","tau"])
    parser.add_argument("folder",type=Path)
    parser.add_argument("--case",type=int)
    parser.add_argument("--env-only",action="store_true")
    args=parser.parse_args()
    result=ecom(args.folder,args.case) if args.suite=="ecom" else tau(args.folder,args.env_only)
    print(json.dumps(result,ensure_ascii=False))
