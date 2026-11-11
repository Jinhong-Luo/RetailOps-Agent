"""仅在显式选择桥接配置时覆盖裁判模型；保留上游评分代码。"""
import json
import os
import sys

_raw = os.environ.get("RETAILOPS_JUDGE_OVERRIDE")
if _raw:
    try:
        import tau2.config as _config
        from tau2.evaluator import evaluator_nl_assertions as _nl
        _override = json.loads(_raw)
        _config.DEFAULT_LLM_NL_ASSERTIONS = _override["model"]
        _config.DEFAULT_LLM_NL_ASSERTIONS_ARGS = dict(_override["model_args"])
        _config.DEFAULT_SEMANTIC_COMMUNICATE_MODEL = _override["model"]
        _config.DEFAULT_SEMANTIC_COMMUNICATE_MODEL_ARGS = dict(_override["model_args"])
        # tau2包初始化会先导入裁判，因此同步更新模块中已复制的常量。
        _nl.DEFAULT_LLM_NL_ASSERTIONS = _override["model"]
        _nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = dict(_override["model_args"])
        if os.environ.get("RETAILOPS_OFFICIAL_SERVICE") == "1":
            from pathlib import Path
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from official_service import register
            register()
            if os.environ.get("RETAILOPS_CUSTOMER_BUTTON") == "1":
                from customer_button import register_button
                register_button()
    except Exception as _error:
        print("裁判模型配置失败，停止运行：" + type(_error).__name__, file=sys.stderr, flush=True)
        # Python默认吞掉启动钩子异常；显式退出防止错误调用其他模型。
        os._exit(78)
