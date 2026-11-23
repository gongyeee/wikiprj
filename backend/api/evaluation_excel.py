"""Bounded XLSX import and literal-text review workbooks (never executable formulas)."""
import io
import json
import math
import zipfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .evaluation_data import FIELDS, MAX_ROWS

LABELS = {"queued": "排队中", "running": "运行中", "completed": "已完成", "cancelled": "已取消", "interrupted": "已中断",
          "pending": "待审查", "scored": "已评分", "answer_failed": "回答失败", "score_failed": "评分失败",
          "accepted": "评分合理", "adjusted": "已调整", "rerun": "需重跑", "answer": "普通问答", "refusal": "应拒答"}


def label(value):
    return LABELS.get(value, value)


def read_upload(upload, sheet=None, header_row=1, mapping=None):
    if not upload or not upload.name.lower().endswith(".xlsx") or upload.size > 8 * 1024 * 1024:
        raise ValueError("请上传不超过 8 MB 的 .xlsx 文件")
    raw = upload.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(info.file_size for info in archive.infolist()) > 40 * 1024 * 1024 or len(archive.infolist()) > 1000:
            raise ValueError("工作簿解压后过大，请拆分文件")
    wb = load_workbook(io.BytesIO(raw), data_only=False, keep_links=False)
    try:
        if sheet is None:
            return {"sheets": wb.sheetnames}
        if sheet not in wb.sheetnames or not 1 <= header_row <= 100:
            raise ValueError("工作表或表头行无效")
        ws = wb[sheet]
        if ws.max_row > MAX_ROWS + header_row or ws.max_column > 100:
            raise ValueError("每个工作表最多 2000 条数据、100 列，请拆分文件")
        merged_values = {}
        for area in ws.merged_cells.ranges:
            if area.max_row > MAX_ROWS + header_row or area.max_col > 100:
                raise ValueError("合并区域超出允许范围")
            cell = ws.cell(area.min_row, area.min_col)
            for row in range(area.min_row, area.max_row + 1):
                for col in range(area.min_col, area.max_col + 1):
                    merged_values[row, col] = cell

        def cell_at(row, col):
            return merged_values.get((row, col), ws.cell(row, col))

        headers = [{"column": col, "label": str(cell_at(header_row, col).value or "").strip()} for col in range(1, ws.max_column + 1)]
        labels = {**FIELDS, "project": "所属项目"}
        aliases = {"expected": {"预期回答", "标准答案"}, "project": {"项目", "项目ID"}}
        suggested = {key: next((h["column"] for h in headers if h["label"] in {label, key, *aliases.get(key, set())}), "") for key, label in labels.items()}
        if mapping is None:
            return {"headers": headers, "mapping": suggested, "sheets": wb.sheetnames}
        if not mapping.get("question"):
            raise ValueError("请映射问题列")
        columns = {key: int(value) for key, value in mapping.items() if key in labels and value}
        if any(col < 1 or col > ws.max_column for col in columns.values()) or len(set(columns.values())) != len(columns):
            raise ValueError("字段映射存在重复或无效列")
        rows = []
        for row in range(header_row + 1, ws.max_row + 1):
            values = {key: cell_at(row, col) for key, col in columns.items()}
            if not any(cell.value is not None for cell in values.values()):
                continue
            record = {key: str(cell.value) if cell.value is not None else "" for key, cell in values.items()}
            record.update(_row=row, _sheet=sheet)
            if any(cell.data_type == "f" for cell in values.values()):
                record["question"] = ""
                record["_error"] = "映射字段包含公式，请转为文本后导入"
            rows.append(record)
        return rows
    finally:
        wb.close()


def literal_row(ws, values):
    # Text from models and uploaded cells must never become spreadsheet formulas.
    ws.append([None] * len(values))
    for col, value in enumerate(values, 1):
        cell = ws.cell(ws.max_row, col)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        cell.value = value
        if isinstance(value, str):
            cell.data_type = "s"


def style_sheet(ws, widths):
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.showGridLines = False
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    for row in ws:
        for cell in row:
            cell.font = Font(name="Calibri", size=11, color="172B4D")
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        if row[0].row == 1:
            for cell in row:
                cell.fill = PatternFill("solid", fgColor="203E62")
                cell.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
            ws.row_dimensions[1].height = 32
        else:
            lines = max(sum(max(1, math.ceil(sum(2 if ord(c) > 255 else 1 for c in line) / max(8, widths[i] - 3))) for line in str(cell.value or "").split("\n")) for i, cell in enumerate(row))
            ws.row_dimensions[row[0].row].height = min(409, max(30, lines * 16 + 12))
            if row[0].row % 2 == 0:
                for cell in row:
                    cell.fill = PatternFill("solid", fgColor="F1F5FA")
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A3
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = "1:1"


def workbook_bytes(wb):
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    return output.getvalue()


def template_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "标准问答"
    literal_row(ws, ["所属项目", *FIELDS.values()])
    style_sheet(ws, [25, 20, 20, 55, 75, 25, 35, 45, 20, 40, 18])
    notes = wb.create_sheet("填写说明")
    literal_row(notes, ["字段", "说明"])
    for values in [("所属项目", "填写已存在的项目 ID 或唯一项目名称；留空使用导入时选择的项目。"),
                   ("问题 / 预期答案", "普通问答必填；保留编号与换行，不使用公式。"),
                   ("用例类型", "普通问答 或 应拒答；应拒答可填写预期拒答行为。"),
                   ("状态", "启用、停用、待核对；留空默认启用。"),
                   ("引用", "多个文档逐行填写；路径优先使用当前项目的文档相对路径。"),
                   ("限制", "每批最多 2000 条；导入前预览，不自动创建项目；问题重复默认跳过。")]:
        literal_row(notes, values)
    style_sheet(notes, [25, 100])
    return workbook_bytes(wb)


def text_parts(text, size=280):
    # Limit both characters and newlines so printed full-answer cells remain readable.
    parts, part = [], ""
    for char in str(text or ""):
        part += char
        if len(part) >= size or part.count("\n") >= 8:
            parts.append(part)
            part = ""
    if part or not parts:
        parts.append(part)
    return parts


def report_bytes(run, results, summary):
    wb = Workbook()
    ws = wb.active
    ws.title = "评测汇总"
    literal_row(ws, ["项目", "值"])
    for row in [("项目名称", run.project_name), ("任务编号", str(run.id)), ("状态", label(run.status)),
                ("创建时间", run.created_at.isoformat()), ("创建人", run.created_by),
                ("总题数", summary["total"]), ("已评分", summary["scored"]), ("失败", summary["failed"]),
                ("未完成", summary["unfinished"]), ("自动平均分（仅已评分）", summary["average"]),
                ("自动通过率（分母为全部题目）", summary["pass_rate"]),
                ("人工已复核", summary["reviewed"]), ("复核覆盖率", summary["review_coverage"]),
                ("人工确认平均分（仅已复核）", summary["human_average"]),
                ("人工通过率（仅已复核）", summary["human_pass_rate"]),
                ("正确拒答率（分母为全部拒答题）", summary["refusal_accuracy"]),
                ("拒答类题数", summary["refusal_count"]),
                (f"文档 Recall@{run.config.get('limit', 8)}", summary["recall_at_k"]),
                (f"文档 Hit@{run.config.get('limit', 8)}", summary["hit_at_k"]),
                ("引用标注有效题数", summary["recall_sample_count"]),
                ("知识库变化", summary["index_changed"]), ("错误", run.error),
                ("运行配置（不含密钥）", run.config),
                ("审查说明", "答案全文对照页保留完整问题、预期回答、实际回答及评分解释，长文本按序续行。逐题审查页的人工列可线下填写；网页复核结果可再次导出，暂不支持报告回传。")]:
        literal_row(ws, row)
    style_sheet(ws, [42, 100])
    for row in ws.iter_rows(min_row=2):
        if any(term in str(row[0].value) for term in ("率", "Recall@", "Hit@")) and isinstance(row[1].value, (int, float)):
            row[1].number_format = "0.0%"
    detail = wb.create_sheet("逐题审查")
    literal_row(detail, ["结果ID", "分类", "场景", "类型", "执行状态", "正确性/50", "完整性/30", "证据/20", "自动总分", "自动通过", "审查结论", "人工评分", "审查意见", "复核人", "复核时间", "引用核验", "问答耗时ms", "人工通过"])
    full = wb.create_sheet("答案全文对照")
    literal_row(full, ["结果ID", "续行序号", "问题", "预期回答", "实际回答", "评分理由 / 错漏 / 运行错误"])
    evidence = wb.create_sheet("实际证据")
    literal_row(evidence, ["结果ID", "续行序号", "实际送入回答模型的证据原文", "标准引用 / 检索结果"])
    audit = wb.create_sheet("复核记录")
    literal_row(audit, ["结果ID", "序号", "结论", "人工评分", "意见", "复核人", "时间"])
    for result in results:
        score, review = result.score, result.review
        literal_row(detail, [result.pk, result.snapshot.get("category"), result.snapshot.get("scene"), label(result.snapshot.get("kind")), label(result.status),
                             score.get("correctness"), score.get("completeness"), score.get("support"), score.get("total"), ("通过" if score["passed"] else "不通过") if "passed" in score else "未评分",
                             label(review.get("status", "pending")), review.get("score"), review.get("comment", ""), review.get("by", ""), review.get("at", ""),
                             score.get("reference_status", "未核验"), result.metrics.get("qa_ms"), ("通过" if review["passed"] else "不通过") if review.get("passed") is not None else "未复核"])
        reason = "\n".join(["评分理由：" + score.get("reason", "未评分"), "遗漏：" + "；".join(score.get("missing_points", [])),
                            "错误：" + "；".join(score.get("wrong_points", [])), "关键事实错误：" + ("是" if score.get("critical_error") else "否"),
                            "评分要点：" + result.snapshot.get("rubric", ""), "运行错误：" + result.error])
        columns = [text_parts(result.snapshot.get("question")), text_parts(result.snapshot.get("expected")), text_parts(result.answer),
                   text_parts(reason)]
        for i in range(max(map(len, columns))):
            literal_row(full, [result.pk, i + 1, *(p[i] if i < len(p) else "" for p in columns)])
        context = text_parts(result.evidence.get("context", ""))
        refs = text_parts(json.dumps({"标准引用": result.snapshot.get("reference_check"), "引用名称": result.snapshot.get("reference_names"), "引用路径": result.snapshot.get("reference_paths"), "检索结果": result.evidence.get("search_results", [])}, ensure_ascii=False, indent=2))
        for i in range(max(len(context), len(refs))):
            literal_row(evidence, [result.pk, i + 1, context[i] if i < len(context) else "", refs[i] if i < len(refs) else ""])
        for i, item in enumerate(review.get("history", []), 1):
            for j, comment in enumerate(text_parts(item.get("comment", ""))):
                literal_row(audit, [result.pk, f"{i}.{j + 1}", label(item.get("status")), item.get("score"), comment, item.get("by"), item.get("at")])
    style_sheet(detail, [14, 20, 20, 15, 20, 16, 16, 16, 16, 16, 20, 16, 50, 20, 28, 20, 18, 18])
    style_sheet(full, [12, 12, 42, 62, 62, 62])
    style_sheet(evidence, [12, 12, 100, 80])
    style_sheet(audit, [12, 12, 20, 16, 80, 22, 30])
    return workbook_bytes(wb)
