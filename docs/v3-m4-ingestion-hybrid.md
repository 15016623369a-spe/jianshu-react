# V3 M4：文档格式与检索基础

日期 2026 年 11 月 19 日。对应产品执行计划的 M4，当前完成第一段。

## 已完成

### 1. PDF / DOCX 入库

在 Markdown/HTML 之外新增：

```text
PDF   → 每页一个引用片段，locator=page
DOCX  → 按标题/段落切分，locator=paragraph
```

- PDF 使用 `pypdf` 提取文本；无文本（扫描件）明确失败；
- DOCX 使用 `python-docx`，保留标题层级、段落和表格；
- 引用保留文件、版本、页/段落定位和原文；
- 文档库和解析预览接受 `.pdf/.docx`；
- 依赖加入 `requirements-step8.txt`。

### 2. 可插拔混合检索

新增：

```text
app/rag/hybrid.py
  HybridIndex
  reciprocal_rank_fusion
```

- `dense=None` 时严格等于现有 BM25 基线；
- 可注入 dense provider，候选经 RRF 融合；
- 可选 reranker 对融合结果重排；
- candidate_k 控制候选规模。

当前产品默认仍是 BM25；没有捆绑模型，也没有把未评测的 dense 设为默认。

### 3. retail domain

新增：

```text
app/retail_investigation.py
DomainRegistry: retail
UI: 调查任务 → 零售指标
```

首批任务：

```text
2026 年 10 月华南电水壶净营收
2026 年 10 月华南电水壶订单数
2026 年 10 月华南电水壶退款额
2026 年 10 月华南电水壶客单价
```

- 商品、地区、月份和指标缺失时澄清；
- 只执行只读聚合 SQL；
- 结果保存为正式调查记录；
- 执行证据包含 SQL。

### 4. manual 独立留出集

新增：

```text
evaluation/manual_holdout_cases.json
evaluation/run_manual_holdout.py
tests/test_manual_holdout.py
```

固定 12 题，覆盖：

- v1/v2 版本差异；
- Wi-Fi、访客网络、电量；
- 不支持的问题；
- 未知版本；
- 问题中显式指定版本。

结果：

```text
manual-holdout-v1: 12/12 passed
```

### 5. 数据源

政策调查继续使用 SQLite 演示账本；现有 PostgreSQL 连接和只读约束未改。

## 验收

```text
Ran 290 tests in 14.242s
OK
manual-holdout-v1: 12/12 passed
```

新增覆盖：

- PDF 页定位；
- DOCX 标题/段落/表格定位；
- DOCX 手册上传与引用；
- PDF 预览；
- BM25 基线等价；
- dense + RRF；
- reranker 重排；
- 候选规模控制。

## 尚未完成

```text
[ ] policy 检索迁移到通用 RetrievalService
[ ] 真实 dense/rerank 配置与同题集指标
[ ] SQLite/PostgreSQL 同一固定数据集的基准对照
[ ] 150 题多域评测和发布门槛（M5）
```

## 结论

> **M4 先落地了“四种文档格式 + 可插拔混合检索接口”；默认仍是可解释、可回归的 BM25，dense/rerank 只有注入并评测后才启用。**
