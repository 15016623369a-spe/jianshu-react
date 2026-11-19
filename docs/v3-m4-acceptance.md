# V3 M4 验收记录

日期 2026 年 11 月 19 日。对应产品执行计划 M4 的四项验收。

## 验收结论

```text
M4 格式       PASS
M4 检索排序   PASS（CrossEncoder 为唯一未测项）
M4 业务域     PASS
M4 数据源     PASS（固定演示数据集）
M4 整体       CONDITIONAL PASS
```

条件：CrossEncoder rerank 有代码路径，但本机没有离线 reranker 模型，未产生真实 CrossEncoder 指标。

## 1. 文档格式

### 验收要求

四种格式各准备带已知答案的样本，均能从引用回到支持答案的位置。

### 证据

```text
Markdown  手册/政策上传、回答、文件+行号引用
HTML      政策/手册上传、标题切分、回答、文件+行号引用
PDF       manual-sample.pdf 上传、回答、file+page 引用
DOCX      标题/段落/表格上传、回答、file+paragraph 引用
```

测试：

```text
tests/test_policy_documents.py
  test_html_manual_upload_is_parsed_and_cited
  test_pdf_manual_upload_answers_with_page_citation
  test_docx_manual_upload_keeps_paragraph_locator
  test_html_policy_can_drive_the_policy_investigation
tests/test_rag_binary_ingest.py
  PDF 页定位
  DOCX 标题/段落/表格
```

结论：**PASS**。

## 2. 检索与排序

### 验收要求

固定 BM25 基线；分别加入 dense、metadata filter、rerank；按相同题集比较召回、引用质量与延迟。

### 证据

manual 12 题 ×3 轮：

```text
BM25    Recall@1/3/5 = 100% / 100% / 100%
        median 0.011 ms
Dense   Recall@1/3/5 = 100% / 100% / 100%
        median 32.47 ms
Rerank  Recall@1/3/5 = 100% / 100% / 100%
        median 0.020 ms（query 缓存后）
        p95    33.18 ms
```

BM25 metadata/ACL 测试：

```text
tests/test_rag_foundation.py
tests/test_rag_retrieval.py（metadata/ACL 用例）
```

policy 候选排序已切到共享 BM25，保留精确标题/引用链：

```text
fixed   72/90
lexical 75/90
auto    75/90（离线无模型）
首轮 Recall@5 约 87.2%–89.4%
```

结论：**PASS（BM25/dense/embedding rerank）**。  
CrossEncoder 真实指标：**未测**，没有伪造结果。

## 3. 业务域

### 验收要求

同一入口完成政策调查、手册问答、零售指标；增长 retail 后前两域原有用例仍通过。

### 证据

```text
policy   A1/A2/A3/A4/A5/A6/A7/A8/A9 回归
manual   manual-holdout-v1: 12/12 passed
retail   净营收 / 订单数 / 退款额 / 客单价
入口     调查任务：政策计算 / 手册问答 / 零售指标
```

retail 示例：

```text
2026 年 10 月华南电水壶净营收
→ ¥274.00
→ 3 笔订单
→ SQL 1 次
```

结论：**PASS**。

## 4. 数据源

### 验收要求

同一小规模固定数据集验证 SQLite 与 PostgreSQL 的日期边界、空值和整数分聚合。

### 证据

```text
evaluation/run_cross_db_benchmark.py
evaluation/results/cross_db_benchmark.json

4 个零售聚合场景：
SQLite = PostgreSQL
订单数/毛营收/退款/净营收全部一致
```

结论：**PASS（固定演示数据集）**。  
生产 PostgreSQL 连接、TLS 和容量不在 M4 范围内，仍属 M6。

## 5. 回归

```text
Ran 296 tests in 15.540s
OK

浏览器 smoke：9/9 PASS
manual-holdout-v1: 12/12 PASS
```

## 6. M4 未覆盖

```text
[ ] CrossEncoder rerank 真实指标
    代码路径：app/rag/dense.py CrossEncoderReranker
    缺少：本机 reranker 模型
[ ] 150 题多域评测（M5，不属于 M4）
```

## 结论

> **M4 的格式、检索、业务域、数据源四项验收通过；唯一保留项是 CrossEncoder rerank 真实模型指标，已明确记录为未测，没有伪造验收结果。**
