"""Regression tests for the generic V3 retrieval foundation."""
from __future__ import annotations

import unittest

from app.rag import (
    BM25Index, DocumentMetadata, Passage, chunk_document, content_sha256, parse_html, parse_markdown, tokenize,
)


def metadata(title="退款政策", source="policy", **attributes):
    return DocumentMetadata(
        document_id="doc-1", title=title, source=source, version=2,
        tenant="demo", acl=frozenset({"alice"}), attributes=attributes,
    )


class IngestTests(unittest.TestCase):
    def test_markdown_keeps_heading_line_bounds(self):
        text = "# 主标题\n\n## 退款口径\n\n净营收 = 订单金额 - 退款。\n\n## 退款上限\n\n每单最多扣 5 元。\n"
        passages = parse_markdown(text, metadata())
        self.assertEqual([item.section for item in passages], ["退款口径", "退款上限"])
        self.assertEqual((passages[0].line_start, passages[0].line_end), (3, 5))
        self.assertIn("净营收", passages[0].text)
        self.assertEqual(passages[1].metadata.acl, frozenset({"alice"}))
        self.assertEqual(passages[0].evidence_id, "doc:doc-1#3-5")

    def test_markdown_without_headings_is_one_passage(self):
        passages = parse_markdown("# 标题\n\n只有正文。\n", metadata())
        self.assertEqual(len(passages), 1)
        self.assertEqual(passages[0].text, "只有正文。")

    def test_html_extracts_headings_and_ignores_script(self):
        text = "<h1>手册</h1><h2>连接 Wi-Fi</h2><p>指示灯为绿色。</p><script>忽略</script><h2>电量</h2><p>查看电池图标。</p>"
        passages = parse_html(text, metadata("手册"))
        self.assertEqual([item.section for item in passages], ["连接 Wi-Fi", "电量"])
        self.assertNotIn("脚本", "\n".join(item.text for item in passages))
        self.assertIn("绿色", passages[0].text)
        self.assertGreaterEqual(passages[0].line_start, 1)

    def test_plain_text_and_format_dispatch(self):
        passages = chunk_document("第一段\n\n第二段", metadata(), format="text")
        self.assertEqual(len(passages), 2)
        with self.assertRaises(ValueError):
            chunk_document("x", metadata(), format="pdf")

    def test_content_hash_is_stable_for_text_and_bytes(self):
        self.assertEqual(content_sha256("abc"), content_sha256(b"abc"))
        self.assertNotEqual(content_sha256("abc"), content_sha256("abd"))


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.passages = (
            Passage("a", 1, "退款政策", "退款口径", "净营收等于订单金额减去退款。", 1, 2, metadata("退款政策")),
            Passage("b", 1, "退款政策", "退款上限", "每单最多扣五元。", 3, 4, metadata("退款政策")),
            Passage("c", 1, "产品手册", "连接 Wi-Fi", "打开设置并选择 M1-Setup。", 1, 2,
                    metadata("产品手册", source="manual")),
        )
        self.index = BM25Index(self.passages)

    def test_cjk_bigrams_and_ascii_tokenization(self):
        terms = tokenize("净营收 Wi-Fi M1")
        self.assertIn("净营", terms)
        self.assertIn("营收", terms)
        self.assertIn("wi", terms)
        self.assertIn("m1", terms)

    def test_search_returns_ranked_citable_hits(self):
        hits = self.index.search("退款上限 五元")
        self.assertEqual(hits[0].passage.document_id, "b")
        self.assertGreater(hits[0].score, 0)
        self.assertEqual(hits[0].passage.evidence_id, "doc:b#3-4")

    def test_metadata_filter_and_acl(self):
        self.assertEqual(self.index.search("设置", filters={"source": "manual"})[0].passage.document_id, "c")
        self.assertEqual(self.index.search("设置", acl=frozenset({"bob"})), [])
        self.assertEqual(len(self.index.search("设置", acl=frozenset({"alice"}))), 1)

    def test_empty_query_and_top_k(self):
        self.assertEqual(self.index.search("   "), [])
        self.assertEqual(len(self.index.search("退款", top_k=1)), 1)
        with self.assertRaises(ValueError):
            self.index.search("退款", top_k=0)


class TitleRetrievalTests(unittest.TestCase):

    def test_title_only_term_can_rank_without_body_occurrence(self):
        passages = (
            Passage("a", 1, "退款上限", "正文", "正文没有关键词。", 1, 1, metadata("退款上限")),
            Passage("b", 1, "产品手册", "正文", "正文没有关键词。", 2, 2, metadata("产品手册", source="manual")),
        )
        hits = BM25Index(passages).search("退款上限")
        self.assertEqual(hits[0].passage.document_id, "a")
