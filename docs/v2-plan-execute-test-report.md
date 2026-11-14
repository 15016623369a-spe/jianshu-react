# 见数 V2 · Plan-and-Execute 对照测试

## 实验设计

2026 年 11 月 15 日，使用 DeepSeek V4 Pro、温度 0，对同一个订单、政策和公告任务运行三种方式。固定流程、ReAct 和 Plan-and-Execute 共用只读工具、整型分计算、范围与预算限制和最终证据核验。固定流程和政策问数继续作为默认。

- **固定流程**由程序依次发现退款组、查询政策、跟进公告和汇总金额。全程无需模型。
- **ReAct**由模型根据工具结果逐次选择下一步和证据 ID。
- **Plan-and-Execute**先由模型生成结构化工具计划。执行器填入实际发现的分组和引用 ID，跳过不适用的步骤。初始计划无效或结束时缺证据，允许最多一次重规划，复用已经得到的证据。数据源确认缺失时直接拒答。两个实验模式共用 35 秒的执行预算。

所有路径都要求核对范围、分组金额、逐笔退款扣减、SQL 和引用。工具层只接受参数化只读查询。Plan-and-Execute 的模型输出仅包含允许的工具及公告层数，分组和文档 ID 由执行器按证据绑定。

## 样本与判分

原 24 题用于复现已有回归。新增 21 道留出请求，与之前的开发集、诊断集和 24 题留出集按日期与地区去重。新题中有 4 道直接政策、4 道一层公告、5 道两层公告、2 道无退款、4 道应拒答及 2 道应澄清。参考结果由独立代码读取合成订单和政策逐笔计算。

每组每批跑三轮，每轮交错执行各模式，单批 5 个工作线程。两批并行运行。延迟包含模型等待，排除本地排队，按同一批内比较。P95 用 nearest-rank，百分比的分母是三轮实际执行次数。判分核对金额和完整证据、拒答原因、澄清处理及模式是否实际运行。

## 实测结果

| 新留出集，21 题 × 3 轮 | 固定流程 | ReAct | Plan-and-Execute |
| --- | ---: | ---: | ---: |
| 通过 | 63/63 | 63/63 | 63/63 |
| 其中金额题 | 45/45 | 45/45 | 45/45 |
| 整体 P95 | 5.49 ms | 5.00 s | 1.37 s |
| 模型调用 | 0 | 234 | 57 |
| Token | 0 | 115,461 | 20,577 |
| SQL 执行 | 135 | 135 | 135 |

新留出集每组均通过 15/15 次两层公告题、12/12 次预期拒答和 6/6 次澄清。Plan-and-Execute 相比 ReAct 少 **75.6% 模型调用、82.2% token**，P95 降低 **72.6%**。57 次有效日期请求各调用一次模型，澄清无需调用。三组均没有无效动作、重复动作或预算超限。正常实测没有触发重规划，本节成绩来自通过结构校验的初始计划。

原 24 题复测每组均 **72/72**，都执行了 150 次 SQL。固定流程、ReAct、Plan-and-Execute 的 P95 分别为 **17.57 ms、5.18 s、1.46 s**。ReAct 调用模型 279 次、135,563 token，Plan-and-Execute 为 66 次、23,826 token。

固定流程在这批题同样满分且延迟最低，因此继续作为默认。Plan-and-Execute 适合留在多源实验入口，已测到的收益是减少模型决策开销。

[新冻结题集](../evaluation/multisource_plan_holdout.json) · [新题运行配置](../evaluation/baselines/multisource_plan_holdout_20261115_run.json) · [新题结果](../evaluation/baselines/multisource_plan_holdout_20261115.json) · [原题复测配置](../evaluation/baselines/multisource_plan_reserve_20261115_run.json) · [原题复测结果](../evaluation/baselines/multisource_plan_reserve_20261115.json)

## 重规划探针

另做故障注入。默认样例第一份计划只允许跟进一层公告，程序完成已有证据的三个组后发现第四组仍需一份新公告。真实 DeepSeek V4 Pro 收到当前证据后规划补读公告、汇总剩余组，最终返回 **¥810.40**。共执行 **5 次 SQL**，已有结果未重复查询。该探针只有重规划是实际模型调用，使用 **661 token**。第一份计划由本地注入，探针与留出集分别计分。[探针轨迹](../evaluation/baselines/multisource_plan_replan_probe_20261115.json)

## 范围与复跑

两批试验中，**123 次模型规划均给出相同的工具顺序**。运行时读到的证据决定具体分组、公告和是否跳过阶段。此次数据支持固定业务任务上从逐步决策改为一次规划的开销差异。开放自然语言任务、跨地区退款上限的反事实计算以及模型按业务目标生成不同计划的能力仍待测试。

打开 [多源问数实验](http://127.0.0.1:28501/?workspace=multisource)，选择「Plan-and-Execute 实验」，保留默认日期和全部地区，验收 **¥810.40**。展开执行步骤可核对结构化计划、真实执行、模型调用和证据。

```bash
python -m evaluation.run_multisource_comparison --split plan-holdout --modes fixed react plan --env-file /path/to/.env --workers 5 --repeats 3 --output-dir /path/to/results
python -m evaluation.run_multisource_comparison --split reserve --modes fixed react plan --env-file /path/to/.env --workers 5 --repeats 3 --output-dir /path/to/reserve-results
python -m unittest discover -s tests -q
```

203 项自动测试全部通过，涵盖单次重规划、禁止模型传 SQL 或未观察 ID、缺证据拒答、输入澄清和 API 默认行为。
