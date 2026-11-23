import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api.ingestion_files import scan_ingestion_sources, receive_upload
from api.document_conversion import convert_to_markdown
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from tools.ingest_project import discover_documents
from api.search_index import (
    build_project_index,
    connect_index,
    get_meta,
    PARSER_VERSION,
    read_indexable_document,
)


def write_workbook(path, rows):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "交易参数"
    for row in rows:
        sheet.append(row)
    book.save(path)


class ExcelIngestionTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw" / "sources"
        self.raw.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def make_project(self, **extra):
        return {"id": str(self.root), "path": str(self.root), "rawDir": str(self.raw), **extra}

    def test_workbook_preserves_sheet_headings_and_markdown_tables(self):
        source = self.raw / "参数表.xlsx"
        write_workbook(source, [
            ["交易所", "产品", "最小价格变动单位"],
            ["上交所", "产品A", 0.01],
            ["深交所", "产品B", 0.001],
        ])
        from openpyxl import load_workbook
        book = load_workbook(source)
        book.create_sheet("第二表").append(["补充说明"])
        book.save(source)
        book.close()
        markdown = convert_to_markdown(source)
        self.assertIn("## 交易参数", markdown)
        self.assertIn("| 交易所 | 产品 | 最小价格变动单位 |", markdown)
        self.assertRegex(markdown, r"\| 上交所 \| 产品A \| 0\.010* \|")
        parsed = read_indexable_document(source)
        self.assertEqual([s["heading"] for s in parsed["sections"]], ["交易参数", "第二表"])
        self.assertTrue(any(b["node_type"] == "table" for b in parsed["blocks"]))

    def test_empty_sheet_still_produces_heading(self):
        source = self.raw / "空表.xlsx"
        write_workbook(source, [])
        parsed = read_indexable_document(source)
        self.assertEqual(parsed["sections"][0]["heading"], "交易参数")

    def test_workbook_is_indexed(self):
        source = self.raw / "参数表.xlsx"
        write_workbook(source, [["交易所", "产品"]] + [["上交所", f"产品{index}"] for index in range(40)])
        result = build_project_index(self.make_project())
        self.assertTrue(result["ok"], result)
        connection = connect_index(self.make_project())
        try:
            suffixes = [row["suffix"] for row in connection.execute("SELECT suffix FROM documents")]
            self.assertEqual(suffixes, [".xlsx"])
            self.assertGreater(connection.execute("SELECT count(*) FROM chunks").fetchone()[0], 0)
        finally:
            connection.close()

    def test_upload_scan_cli_and_index_share_supported_formats(self):
        source = self.raw / "参数表.xlsx"
        write_workbook(source, [["交易所", "产品"], ["上交所", "产品A"]])
        data = source.read_bytes()
        with override_settings(BASE_DIR=self.root):
            refs = receive_upload(self.make_project(),
                                  [SimpleUploadedFile("参数表.xlsx", data)], ["参数表.xlsx"])
        self.assertEqual(refs["receivedFiles"], 1)
        scan = scan_ingestion_sources(self.make_project())
        self.assertEqual(len(scan["files"]), 2)
        self.assertEqual(len(discover_documents(self.raw, 0)), 2)
        result = build_project_index(self.make_project())
        self.assertTrue(result["ok"], result)
        connection = connect_index(self.make_project())
        try:
            self.assertEqual(get_meta(connection, "parser_version"), PARSER_VERSION)
            chunks = connection.execute("SELECT content,start_offset,end_offset,document_id FROM chunks").fetchall()
            self.assertTrue(chunks)
            for chunk in chunks:
                text = connection.execute("SELECT extracted_text FROM documents WHERE id=?", (chunk["document_id"],)).fetchone()[0]
                self.assertEqual(chunk["content"], text[chunk["start_offset"]:chunk["end_offset"]])
        finally:
            connection.close()

    def test_corrupt_workbook_is_reported_and_does_not_publish_partial_index(self):
        (self.raw / "损坏.xlsx").write_bytes(b"not a workbook")
        (self.raw / "说明.txt").write_text("有效正文", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Markdown"):
            convert_to_markdown(self.raw / "损坏.xlsx")
        result = build_project_index(self.make_project())
        self.assertFalse(result["ok"])
        self.assertIn("Markdown", result["failures"][0]["error"])
        connection = connect_index(self.make_project())
        self.assertIsNone(connection)

    def test_text_csv_and_html_use_the_same_conversion_boundary(self):
        for name, content, expected in [
            ("说明.md", "# 标题\n\n正文", "# 标题"),
            ("说明.txt", "中文正文", "中文正文"),
            ("参数.csv", "交易所,产品\n上交所,产品A", "| 上交所 | 产品A |"),
            ("页面.html", "<h1>标题</h1><p>中文正文</p>", "# 标题"),
        ]:
            with self.subTest(name=name):
                source = self.raw / name
                source.write_text(content, encoding="utf-8")
                self.assertIn(expected, convert_to_markdown(source))

    def test_unreadable_suffixes_are_reported_not_dropped(self):
        write_workbook(self.raw / "参数表.xlsx", [["交易所"], ["上交所"]])
        (self.raw / "旧规则.rtf").write_bytes(b"not really a doc")
        (self.raw / "说明.txt").write_text("正文", encoding="utf-8")
        scan = scan_ingestion_sources(self.make_project())
        self.assertEqual(sorted(item["sourceFile"] for item in scan["files"]), ["参数表.xlsx", "说明.txt"])
        self.assertEqual([item["sourceFile"] for item in scan["unsupported"]], ["旧规则.rtf"])
