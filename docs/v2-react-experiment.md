# 见数 V2 · 受控 ReAct 对照

2026 年 11 月 15 日在本机运行。政策问数界面提供固定流程、ReAct + 快路径、纯 ReAct 三个选项。固定流程是默认，另外两种用于实验。

## 三种运行方式

- **固定 V2**按顺序检索版本、执行每个分段的只读查询，再核对证据和金额。
- **ReAct + 快路径**在且仅在单一政策完整覆盖区间时使用固定流程。跨版本或可能有覆盖缺口的有效日期请求进入 ReAct。
- **纯 ReAct**对日期有效的请求由 DeepSeek V4 Pro 选下一步。日期缺失或顺序错误时，三组都使用相同输入检查。

ReAct 使用 LangGraph 在选动作和执行工具之间循环。工具允许检索适用政策、按已有分段 ID 查询。模型可提交结束动作，最终发布仍需同一套规则 Verifier 核对覆盖、绑定参数、返回字段、分段金额与引用。返回的证据出自实际政策和 SQL。单次最多 6 次动作决策、2 次 SQL 工具派发、累计约 12k prompt token 上限，以及 20 秒执行预算。每次调用前后检查剩余时间，并把它传给底层 I/O 超时。重复或无效动作由服务端拦截，连续无进展或证据不足时停止。页面展示动作和状态。

## 政策问数留出对照

冻结 18 道不同请求，分别包含 6 道单政策、6 道跨政策、6 道澄清或拒答，与首版 15 道开发题的规范化输入互不重叠。在相同合成订单、政策文档、工具、Verifier 上交错跑 3 轮，用 DeepSeek V4 Pro、温度 0、5 个工作线程。判定要求结果类型、日期/地区、金额与来源引用准确，同时核对实际执行路径。每组 54 次执行，合计 162 次。P95 为对应样本池的 nearest-rank，耗时包含模型请求，排除评测队列等待。

| 指标 | 固定 V2 | ReAct + 快路径 | 纯 ReAct |
| --- | ---: | ---: | ---: |
| 每轮通过题数 | 18 / 18 / 18 | 18 / 18 / 18 | 18 / 18 / 18 |
| 总通过次数 | 54 / 54 | 54 / 54 | 54 / 54 |
| 整体 P95 | 3.24 ms | 3.60 s | 3.59 s |
| 单政策题 P95 | 3.77 ms | 4.87 ms | 2.96 s |
| 跨政策题 P95 | 3.67 ms | 4.15 s | 3.64 s |
| 澄清或拒答 P95 | 1.51 ms | 0.97 s | 1.18 s |
| 模型调用 | 0 | 81 | 135 |
| Token | 0 | 26,829 | 42,273 |
| SQL 查询 | 54 | 54 | 54 |

三组均无无效动作、重复动作或预算终止，澄清或拒答题均通过。混合方案比纯 ReAct 少 40% 模型调用、36.5% token，差额全部来自单政策题。两种 ReAct 在跨政策题上各执行 72 次模型调用、共用 24,804 token，正确率一致。按公开峰时费率、实际缓存用量估算，三组总模型费用约为 $0、$0.0125、$0.0195，账单金额取决于实际计价时段。[DeepSeek 公开费率](https://api-docs.deepseek.com/quick_start/pricing/)

本场景的政策工具已经能确定性地给出适用版本和完整的分段。固定路径达到本题集上限，ReAct 多花了时间和 token，未增加正确项。保留两种实验模式。下一次对照需要加入政策与 SQL 的执行顺序取决于前面查到了什么的任务，例如先识别不同产品实际发生的订单，再找各自适用条款。

[冻结留出题](../evaluation/react_holdout.json) ·[运行配置](../evaluation/baselines/react_policy_holdout_20261115_run.json) · [按组结果](../evaluation/baselines/react_policy_holdout_20261115.json)

## 原 V1 SQL 回归

原项目 `43b151a` 的产品 SQL 流程在相同冻结 50 道 BIRD Mini-Dev 开发题用 DeepSeek V4 Pro 真跑一轮。**26/50（52%）**，位于历史三轮 **26–27/50** 区间。50 题都执行了 SQL，无基础设施错误。本次用了两个 worker，历史用了三个，因此不以 P95 判定快慢，也没有运行正式三轮门禁。

将本轮 V1 的 50 条最终 SQL 在 V2 继承路径回放，逐题状态、评分、列和结果行统计 **50/50 一致**。回放固定了 SQL，考察继承执行路径。政策题与 BIRD SQL 分别评分。[V1 回归记录](../evaluation/baselines/react_v1_guard_20261115.json)

## 本机复现

```bash
# 使用自己的本地模型配置路径
python -m evaluation.run_policy_comparison --env-file /path/to/.env --workers 5 --repeats 3
RAG_DEMO_ENV_FILE=/path/to/.env RAG_LLM_MODEL=deepseek-v4-pro bash scripts/demo.sh live
```

不配置密钥时可用 `--modes fixed` 复跑固定对照，或用 `bash scripts/demo.sh` 启动离线界面。三种方式可通过 `POST /api/v2/policy-revenue?mode=fixed|hybrid|react` 调用。使用同一个日期/地区请求体。结果里的 `requested_mode`、`strategy` 和 `tool_trace` 说明实际路径。
