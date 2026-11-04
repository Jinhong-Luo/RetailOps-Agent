# RetailOps-Agent

零售业务 Agent 执行优化与评测驱动自改进，固定复用 **τ-bench v2.0.0**。当前交付阶段是 **A0 原生 LLMAgent 基线复现与评测基础设施**；结构化状态、执行检查、故障恢复和 GEPA 尚未实现，不宣称这些能力或效果提升。

## 当前状态与归属

- 上游提交固定为 `989d3285ae669e49503324bdb9981bf14fccedeb`；默认安装路径使用上游 `uv.lock`。本次服务器实测使用独立解析后冻结的依赖快照，二者不可混报。
- 原生 Agent、业务环境、全部零售工具、用户模拟器、任务评分和批量续跑来自上游。
- 本仓库新增版本校验、模型传输配置、分组清单、同题思考强度矩阵、完整分母汇总与开发集选择规则。
- 候选强度为 `low/medium/high/xhigh/max/ultra`。列表不等于服务端已验证支持；需要真实请求和桥接服务记录。
- 没有实际完整运行的目录不能形成成功率结论。小样本仅验证链路，不能据此选定最佳档位。

## 固定数据与分组

官方入口为 `tau2 run --benchmark text --language zh --domain retail`，共 **50 道中文零售任务**，属于 `text-v1` 的一个单元，不是整个基准。

上游数据来自 `data/tau2/task_subsets/retail_50.json` 和 `data/tau2/multilingual/zh/regional/retail.json`。CLI 接收规范任务 ID，实际轨迹保存本地化任务 ID，例如 `6` 对应 `6_zh_identity`。

`data/splits.json` 在任何策略调优之前按同一用户、订单、规范化任务模板的传递关联分组，当前分为调优集25题、开发集13题、保留集12题。共享商品不作为关联依据，避免所有商品查询合并为一组。分组需要人工复核语义近似模板，**不能直接称为独立泛化测试**。

开发集用于思考强度选择；选定后才运行保留集。官方50题可另作公开基准完整报告，但包含调优题时不能称独立测试。

## 安装与运行

服务器使用 Python 3.12 或 3.13。以下命令均在仓库根目录执行：

```bash
python3 scripts/bootstrap.py --environment measured-snapshot
python3 scripts/prepare_splits.py
python3 -m unittest discover -s tests -v
```

`bootstrap.py` 要求可调用 `uv`。`--environment measured-snapshot` 从 `reports/requirements.resolved.txt` 安装本次实测依赖到项目 `.venv`，再以 `--no-deps -e` 安装固定源码。`--environment upstream-lock` 则使用上游 `uv.lock` 安装到 `vendor/tau2-bench/.venv`，是另一种环境条件。复现实测时请使用干净项目目录，避免两套环境同时存在。首次拉取固定标签并校验提交；后续运行不覆盖已有源码改动。`vendor/`、环境文件和完整运行轨迹不提交 Git。

### 当前 Codex 登录桥接条件

`configs/codex_bridge.json` 为当前实验条件：通过本机 `http://127.0.0.1:18863/v1` 使用现有 Codex 登录。桥接服务必须先启动并提供工具调用兼容接口；认证令牌从本机 `.env` 的 `CODEX_BRIDGE_TOKEN` 读取，不写入实验清单；本仓库不会读取或复制登录凭据。

Agent、用户模拟器和语义裁判均使用 `gpt-6.1-sol`；只有 Agent 强度变化，用户和裁判固定 `medium`。原生 Agent、环境与评分规则保留。**这是 Codex 模型传输适配基线，不能与官方直连 API 运行条件混报。**

上游 NL 断言裁判的模型写在配置常量中；`runtime/sitecustomize.py` 只在显式桥接配置下、每个子进程启动时覆盖模型及参数，并同步更新裁判模块已导入的常量。语义通信裁判另通过上游 CLI 显式传入。覆盖信息记录在每次运行的 `manifest.json`。

```bash
# 只生成计划，不调用模型。
python3 scripts/run_baseline.py --config configs/codex_bridge.json --split dev --effort low --limit 1

# 让上游实际解析冻结任务、语言包和模型配置，不调用模型。
python3 scripts/run_baseline.py --config configs/codex_bridge.json --split dev --effort low --limit 1 --native-dry-run

# 工具往返探测，会真实调用模型；应使用上游独立环境。
vendor/tau2-bench/.venv/bin/python scripts/probe_models.py --config configs/codex_bridge.json --effort low --execute

# 一道开发题的真实连通实验。
python3 scripts/run_baseline.py --config configs/codex_bridge.json --split dev --effort low --limit 1 --execute

# 完整开发集思考强度对照：6档 × 13题 × 1次。
python3 scripts/run_matrix.py --config configs/codex_bridge.json --split dev --execute --output runs/dev-matrix

# 汇总各档，缺失/失败留在原定分母。
python3 scripts/summarize.py runs/dev-matrix/low runs/dev-matrix/medium runs/dev-matrix/high runs/dev-matrix/xhigh runs/dev-matrix/max runs/dev-matrix/ultra --output reports/dev-matrix.json
```

重复同一命令会让上游跳过已完成运行；这不是业务事务恢复。变更配置、任务集或固定版本时必须使用新目录，脚本会拒绝混用。

### 标准 API 条件

若以后使用标准 API，复制 `.env.example` 为 `.env` 后在服务器本地填写，使用默认 `configs/baseline.json`。其用户模型及裁判沿用上游发布配置；Agent 模型默认 `openai/responses/gpt-6.1-sol`，可通过 `RETAILOPS_AGENT_MODEL` 修改实际服务模型标识。模型名称和入口必须来自服务方，不能由 Codex 界面名称推断 API 可用性。

## 评分与档位选择

成功判定仅使用原生 `reward_info.reward == 1`。分母等于运行清单中题数乘试次数，零结果、缺失轨迹和基础设施失败不会被删除。脚本拒绝重复任务试次和计划外任务。

选择仅用于相同配置、相同开发题、相同试次数且所有候选完整完成的结果。预设规则为：距最高成功率不超过2个百分点时，选择累计实际运行时间除以计划任务数较低者，包含本脚本记录的启动、失败重试及续跑耗时。13题一次运行的统计不确定性较大，结论是初步配置选择；需要更多固定种子重复验证稳定性。保留集不参与选优。

报告输出成功数、完整分母、缺失数、终止原因、耗时、工具调用和可观察消息 token。工具错误、重复操作、误拦截和细粒度业务失败归因尚需后续标注，不能从终止原因直接推断。

上游旧 LiteLLM 在无法识别模型价格时返回 `0.0`，因此本仓库将实际费用写为 `null`，等待服务账单核验。已有消息的 token 不覆盖全部裁判和失败重试，不能包装成完整账单成本；Codex 订阅也不能直接按 API 价格计费。

## 原始轨迹与溯源

每档目录包含不可混用的 `manifest.json`、启动参数 `command.json`、退出状态、执行日志及上游 `native/zh-retail/results.json`。完整日志留在服务器，检查后仅提交去敏的汇总报告。

```bash
vendor/tau2-bench/.venv/bin/tau2 view --file runs/dev-matrix/low/native
```

源码与方法来源：[τ-bench v2.0.0](https://github.com/sierra-research/tau2-bench/tree/v2.0.0)、[原生运行说明](https://github.com/sierra-research/tau2-bench/blob/v2.0.0/docs/running-the-benchmark.md)、[原生 LLMAgent](https://github.com/sierra-research/tau2-bench/blob/v2.0.0/src/tau2/agent/llm_agent.py)。上游许可随固定源码保留。

## 仓库内 Codex 登录桥接

桥接代码已随仓库提供，使用服务器已有 Codex ChatGPT 登录，不提取登录令牌。先预检，再做普通回答、虚拟业务工具与 JSON 裁判三个烟测：

```bash
python3 model_bridge/codex_bridge.py --check
python3 model_bridge/codex_bridge.py --port 18861 --runtime-dir /home/luojinghong/LLM_Proj/.bridge-runtime
# 在另一个终端运行；默认只测 low。
python3 model_bridge/probe.py --base-url http://127.0.0.1:18863/v1 --effort low --output reports/bridge-smoke-low.json
```

完整六档链路验证加 `--all-efforts`。输出包含实际思考强度、模型名、耗时、token 与失败分类；它只是接入烟测，不能替代业务基线或据此选出最佳档位。固定标记 `codex-app-server-structured-output-v1`，不称为原生模型 API 等价实验。详细限制、Docker 网关接入和登录问题记录见 [桥接说明](model_bridge/README.md)。
