"""通过基线同一 LiteLLM 传输探测工具调用；探测不属于基准成绩。"""
import argparse
import json
import os
import time
from common import ROOT, agent_args, digest, load_config, load_env, write_json
from run_baseline import child_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--effort", default="all")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", default=str(ROOT / "runs" / "model-probes.json"))
    args = parser.parse_args()
    load_env()
    config = load_config(args.config)
    efforts = config["efforts"] if args.effort == "all" else [args.effort]
    if not args.execute:
        print(json.dumps({"model": config["agent_model"], "efforts": efforts, "说明": "添加--execute才会调用模型"}, ensure_ascii=False))
        return
    os.environ.update(child_env(config))
    import litellm
    # extra_body 负责原样发送强度，未知参数仍要求报错。
    litellm.drop_params = False
    tool = {"type": "function", "function": {"name": "lookup_test", "description": "读取测试对象，仅用于连通检查", "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}}}
    results = []
    for effort in efforts:
        started = time.monotonic()
        entry = {"requested_effort": effort, "accepted": False}
        try:
            messages = [{"role": "user", "content": "请调用lookup_test查询id=probe-001，拿到结果后只回复返回的value。"}]
            reply = litellm.completion(model=config["agent_model"], messages=messages, tools=[tool], tool_choice="required", **agent_args(config, effort))
            calls = reply.choices[0].message.tool_calls or []
            if len(calls) != 1 or calls[0].function.name != "lookup_test" or json.loads(calls[0].function.arguments) != {"id": "probe-001"}:
                raise ValueError("工具调用格式或参数不符")
            messages.append(reply.choices[0].message.model_dump(exclude_none=True))
            messages.append({"role": "tool", "tool_call_id": calls[0].id, "content": '{"value":"探测成功"}'})
            second = litellm.completion(model=config["agent_model"], messages=messages, **agent_args(config, effort))
            if "探测成功" not in (second.choices[0].message.content or ""):
                raise ValueError("工具结果未正确回传")
            entry.update(accepted=True, returned_model=second.model, effective_effort_verified=False, note="请求与工具往返成功；仍需服务端日志证明真实执行强度")
        except Exception as error:
            # 凭据只留在环境，错误报告仅记录异常类型，避免输出服务器回显的秘密。
            entry["error_type"] = type(error).__name__
        entry["duration_seconds"] = time.monotonic() - started
        results.append(entry)
        write_json(args.output, {"config_digest": digest(config), "results": results, "is_benchmark": False})
        print(json.dumps(entry, ensure_ascii=False))
    raise SystemExit(0 if all(x["accepted"] for x in results) else 1)


if __name__ == "__main__":
    main()
