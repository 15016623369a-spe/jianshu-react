"""PDF/DOCX ingestion keeps page and paragraph locators."""
from __future__ import annotations

import unittest
from io import BytesIO

from docx import Document

from app.rag.ingest import parse_docx
from app.rag.models import DocumentMetadata


class BinaryIngestTests(unittest.TestCase):
    def test_docx_headings_and_tables_are_parsed(self):
        buffer = BytesIO()
        document = Document()
        document.add_heading("设备手册", level=1)
        document.add_heading("连接 Wi-Fi", level=2)
        document.add_paragraph("连接成功时指示灯为绿色。")
        document.add_heading("检查电量", level=2)
        document.add_paragraph("在首页查看电池图标。")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "状态"
        table.cell(0, 1).text = "正常"
        document.save(buffer)
        passages = parse_docx(buffer.getvalue(), DocumentMetadata("doc", "手册", source="manual", version=1))
        self.assertGreaterEqual(len(passages), 2)
        self.assertEqual(passages[0].section, "连接 Wi-Fi")
        self.assertEqual(passages[0].locator, "paragraph")
        self.assertIn("绿色", passages[0].text)
        self.assertIn("状态 | 正常", passages[-1].text)

    def test_docx_without_text_fails_closed(self):
        buffer = BytesIO()
        Document().save(buffer)
        with self.assertRaises(ValueError):
            parse_docx(buffer.getvalue(), DocumentMetadata("doc", "empty"))


class PdfIngestTests(unittest.TestCase):
    def test_pdf_pages_keep_page_locators(self):
        from pathlib import Path
        from app.rag.ingest import parse_pdf
        passages = parse_pdf((Path(__file__).with_name("fixtures") / "manual-sample.pdf").read_bytes(),
                             DocumentMetadata("pdf", "manual.pdf", source="manual"))
        self.assertEqual(len(passages), 2)
        self.assertEqual((passages[0].line_start, passages[0].locator), (1, "page"))
        self.assertIn("Connect Wi-Fi", passages[0].text)
        self.assertIn("battery", passages[1].text.casefold())
