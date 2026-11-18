# V3 基础层：通用 ingestion / retrieval / evidence

日期 2026 年 11 月 18 日。本提交从 `v2.0.0` 检查点之后的双域版本继续，新增独立的通用 RAG 基础层。应用行为尚未切换；现有政策与手册端点保持不变。

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

## 本次没有做

```text
没有改 HTTP API
没有改 UI
没有替换现有政策/手册检索
没有引入 Dense/Reranker
没有动 v2.0.0 tag
```

## 验收

```text
Ran 263 tests in 14.509s
OK
```

新增 14 项测试，覆盖：

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

## 下一步

1. 把 `DomainRegistry` 接入 `/api/v3/investigations` 路由；
2. 将 policy/manual 的文档加载接到 `RetrievalService`；
3. 增加 Markdown/HTML 上传入口；
4. 为 manual 域增加独立留出评测；
5. 再评估 Dense/Reranker。
