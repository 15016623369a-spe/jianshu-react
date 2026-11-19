# V3 基础层：通用 ingestion / retrieval / evidence

日期 2026 年 11 月 18 日。本提交从 `v2.0.0` 检查点之后的双域版本继续，新增独立的通用 RAG 基础层。v2 端点保持不变；V3 路由已改为 domain registry 驱动，手册域已接入通用检索，并支持 HTML 政策/手册上传。

## 本次交付

```text
app/rag/
  models.py      DocumentMetadata / Passage / Evidence
  ingest.py      Markdown / HTML / 纯文本解析，标题感知切段
  retrieval.py   BM25 + metadata filter + ACL
  service.py     ingestion → index → citation-ready evidence
  domains.py     domain pack 描述与注册表
tests/
  test_rag_foundation.py
  test_rag_service.py
  test_rag_domains.py
```

## 设计约束

- 基础层不依赖 FastAPI、Streamlit 或现有 policy workflow；
- Markdown 保留 1-based 行号；
- HTML 忽略 script/style，按块级顺序提取；
- evidence_id 形如 `doc:<document_id>#<line_start>-<line_end>`；
- 检索必须显式传 ACL；未授权文档不会命中；
- 空文档/空 query/重复 domain 都 fail closed；
- 中文按单字 + 二元词片段、英文按词切分；
- 标题字段加权，但排序仍确定性。

## 本次接入

```text
GET /api/v3/domains
  返回 policy/manual 的 domain pack 描述与 handler_bound
/api/v3/investigations
  改为 registry 查 domain → 调用已绑定 handler
manual 域
  已改用通用 RetrievalService（BM25 + Evidence + source_file）
  支持上传 HTML 政策和手册，并按标题/段落切分
policy 域
  行为保持不变，仍走原有证据/账本核验；HTML 政策也可上传并解析
UI 已允许政策/手册上传 .html/.htm
没有引入 Dense/Reranker
没有动 v2.0.0 tag
```

## 验收

```text
Ran 292 tests in 15.748s
OK
```

新增 42 项测试，覆盖：

- Markdown 标题行号；
- HTML 提取与 script 忽略；
- 纯文本切段；
- content hash；
- 中文 tokenization；
- BM25 排序；
- metadata filter；
- ACL 拒绝；
- evidence 生成；
- domain 注册与未知 domain fail closed。

## Hybrid 决策

本次没有引入 Dense/Reranker，理由是现有 BM25 尚未被证明不够：

```text
政策 grounding 首轮候选 Recall@5：
  lexical 89.36%
  auto    91.49%
  fixed   87.23%
```

当前门槛是 85%，BM25 达标；因此先不加向量检索。  
触发条件：50+ 独立检索题上 Recall@5 低于 85%，或同义表达明显漏检。

## 下一步

M1/M2 已完成。当前进入 M3：

1. 历史页与导出（Markdown 调查报告、CSV 明细）；
2. 将 policy 的文档检索接到 `RetrievalService`（保留现有引用/账本规则）；
3. 为 HTML 上传增加解析诊断；
4. 为 manual 域增加独立留出评测；
5. 再评估 Dense/Reranker。
