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

### 5. policy 迁移

直接替换为纯 BM25 会破坏精确标题匹配，因此改为：

```text
legacy 排序（标题/引用链）在前
+ 共享 BM25 只补充额外候选
```

冻结离线对照：

```text
fixed   72/90
lexical 75/90
auto    75/90（离线无模型）
```

旧排序基线为同一组 72/75/75；该 adapter 没有降低金额结果，并已把共享检索接入 policy 候选层。  
全量切换仍受精确标题/引用链约束，不能直接删掉 legacy 排序。

### 6. dense/rerank 状态

新增：

```text
app/rag/dense.py
  SentenceTransformerDense
  CrossEncoderReranker
app/rag/hybrid.py
  HybridIndex
  reciprocal_rank_fusion
```

本机用离线 `bge-m3` 对 manual 检索题集做了 12 题 ×3 轮对照：

```text
BM25   Recall@1/3/5 = 100% / 100% / 100%
Dense  Recall@1/3/5 = 100% / 100% / 100%
BM25   median 0.011 ms
Dense  median 32.47 ms
```

结论：当前数据规模下 BM25 已足够，dense 没有召回收益但延迟增加；因此默认仍用 BM25，dense 只在配置并评测后启用。CrossEncoderReranker 代码路径保留，未产生真实 rerank 指标。

### 7. 数据源

用同一批零售订单做 SQLite/PostgreSQL 对照，4 个聚合场景全部一致：

```text
SQLite  = PostgreSQL（订单数、毛营收、退款、净营收）
```

测试脚本：`evaluation/run_cross_db_benchmark.py`；结果：`evaluation/results/cross_db_benchmark.json`。

## 验收

```text
Ran 292 tests in 14.578s
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
[ ] policy 全量切换到共享检索（当前是 legacy + BM25 候选 union）
[ ] 真实 dense/rerank 模型指标（代码路径已备，本机无离线模型）
[ ] SQLite/PostgreSQL 同一固定数据集的基准对照
[ ] 150 题多域评测和发布门槛（M5）
```

## 结论

> **M4 先落地了“四种文档格式 + 可插拔混合检索接口”；默认仍是可解释、可回归的 BM25，dense/rerank 只有注入并评测后才启用。**
