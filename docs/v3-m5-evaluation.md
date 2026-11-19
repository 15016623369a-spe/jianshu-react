# V3 M5：独立评测与发布门槛

日期 2026 年 11 月 19 日。M5 已开始，目标是 150 题多域评测。

## 一、150 题目标

```text
policy  60
manual  45
retail  45
```

当前拆分：

```text
policy 30  现有 grounding holdout
policy 12  现有 investigation holdout
policy 18  M5 新增澄清/拒答/歧义题
manual 45  M5 manual suite（12 基础 + 33 变体/不支持/版本）
retail 45  M5 retail suite
```

## 二、当前可执行 gate

新增：

```text
evaluation/run_m5_gate.py
evaluation/m5/manifest.json
evaluation/m5/manual_cases.json
evaluation/m5/retail_cases.json
evaluation/m5/policy_extra_cases.json
```

当前 gate 直接执行：

```text
policy extra 18
manual       45
retail       45
合计         108
```

结果：

```text
passed            108/108
wrong_numeric     0
citation_precision 100%
abstain_pass      100%
```

另 42 道 policy 基础题由现有 policy grounding/investigation runner 执行，尚未汇总进统一 gate。

## 三、指标定义

```text
错误出数
  金额、方向、范围错误，或应拒答却返回金额

Doc Recall@5
  gold 文档是否出现在前五，按题平均

Citation precision
  返回引用是否来自正确文档、版本和支持段落

引用支持率
  需证据支撑的结论是否被引用充分支持

拒答与版本
  应拒答正确率；错误引用过期版本次数

模型与延迟
  区分确定性路径和模型兜底；P50/P95、模型调用次数
```

## 四、当前验收

```text
Ran 297 tests in 15.644s
OK
M5 gate: 108/108
manual-holdout-v1: 12/12
```

## 五、M5 剩余

```text
[ ] 将 policy 基础 42 题接入统一 gate
[ ] 补齐 150 题统一 manifest/结果
[ ] 逐题记录 expected status、金额/答案、允许引用、版本、可接受澄清
[ ] 计算 Doc Recall@5 和引用支持率
[ ] 区分开发集/留出集，冻结版本后再调参
[ ] 固定硬件的 P50/P95、模型调用与错误率
[ ] 汇总报告、失败分类和发布门槛
```

## 六、结论

> **M5 已建立多域套件骨架和可执行 gate：150 题目标已定义，当前 108 题直接全绿，42 题 policy 基础集待并入统一报告。**
