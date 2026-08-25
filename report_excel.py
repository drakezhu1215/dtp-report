from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from analyzer import ProjectViolations, SEVERITY_LABELS
from chart_builder import SEVERITY_ORDER, SEVERITY_SHORT


HEADER_FILL = PatternFill(start_color="1976D2", end_color="1976D2", fill_type="solid")
HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
DATA_FONT = Font(name="Calibri", size=11)
NUM_FONT = Font(name="Calibri", size=11)
THIN_BORDER = Border(
    left=Side(style="thin", color="CCCCCC"),
    right=Side(style="thin", color="CCCCCC"),
    top=Side(style="thin", color="CCCCCC"),
    bottom=Side(style="thin", color="CCCCCC"),
)
SEV_FILLS = {
    "1": PatternFill(start_color="FFCDD2", end_color="FFCDD2", fill_type="solid"),
    "2": PatternFill(start_color="FFE0B2", end_color="FFE0B2", fill_type="solid"),
    "3": PatternFill(start_color="FFF9C4", end_color="FFF9C4", fill_type="solid"),
    "4": PatternFill(start_color="BBDEFB", end_color="BBDEFB", fill_type="solid"),
    "5": PatternFill(start_color="C8E6C9", end_color="C8E6C9", fill_type="solid"),
}


def _style_header(ws, row: int, max_col: int):
    for col in range(1, max_col + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER


def _style_data_cell(cell, is_num: bool = False):
    cell.font = DATA_FONT
    cell.border = THIN_BORDER
    if is_num:
        cell.alignment = Alignment(horizontal="right")
        cell.number_format = "#,##0"
    else:
        cell.alignment = Alignment(horizontal="left")


def _auto_width(ws, min_width: int = 10, max_width: int = 40):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val = str(cell.value) if cell.value else ""
            max_len = max(max_len, len(val))
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, min_width), max_width)


def generate_excel_report(
    data: list[ProjectViolations],
    output_path: Path,
) -> Path:
    wb = Workbook()
    active = [d for d in data if d.has_data]

    _sheet_overview(wb, active)
    _sheet_severity(wb, active)
    _sheet_category(wb, active)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return output_path


def _sheet_overview(wb: Workbook, active: list[ProjectViolations]):
    ws = wb.active
    ws.title = "Overview"

    headers = ["#", "Project", "Filter", "Build ID", "Failed", "Suppressed", "Total", "Has Data"]
    ws.append(headers)
    _style_header(ws, 1, len(headers))

    for idx, d in enumerate(active, 1):
        row = [idx, d.project_name, d.filter_name, d.build_id, d.failed, d.suppressed, d.failed + d.suppressed, "Yes"]
        ws.append(row)
        for col in range(1, len(headers) + 1):
            _style_data_cell(ws.cell(row=idx + 1, column=col), is_num=(col in (5, 6, 7)))

    total_row = len(active) + 2
    ws.cell(row=total_row, column=1, value="")
    ws.cell(row=total_row, column=2, value="TOTAL")
    ws.cell(row=total_row, column=5, value=sum(d.failed for d in active))
    ws.cell(row=total_row, column=6, value=sum(d.suppressed for d in active))
    ws.cell(row=total_row, column=7, value=sum(d.failed + d.suppressed for d in active))
    for col in range(2, 8):
        c = ws.cell(row=total_row, column=col)
        c.font = Font(name="Calibri", bold=True, size=11)
        c.border = THIN_BORDER
        if col in (5, 6, 7):
            c.number_format = "#,##0"
            c.alignment = Alignment(horizontal="right")

    _auto_width(ws)


def _sheet_severity(wb: Workbook, active: list[ProjectViolations]):
    ws = wb.create_sheet("Severity Breakdown")

    headers = ["Project"] + [SEVERITY_SHORT.get(s, s) for s in SEVERITY_ORDER] + ["Total"]
    ws.append(headers)
    _style_header(ws, 1, len(headers))

    for row_idx, d in enumerate(active, 2):
        if not d.severity_breakdown:
            continue
        ws.cell(row=row_idx, column=1, value=d.project_name)
        _style_data_cell(ws.cell(row=row_idx, column=1))
        for sev_idx, sev in enumerate(SEVERITY_ORDER, 2):
            cnt = d.severity_breakdown.get(sev, 0)
            cell = ws.cell(row=row_idx, column=sev_idx, value=cnt)
            _style_data_cell(cell, is_num=True)
            if sev in SEV_FILLS and cnt > 0:
                cell.fill = SEV_FILLS[sev]
        cell_total = ws.cell(row=row_idx, column=len(headers), value=d.failed)
        _style_data_cell(cell_total, is_num=True)
        cell_total.font = Font(name="Calibri", bold=True, size=11)

    _auto_width(ws)


def _sheet_category(wb: Workbook, active: list[ProjectViolations]):
    ws = wb.create_sheet("Category Breakdown")

    active_with_cat = [d for d in active if d.category_breakdown]
    project_names = [d.project_name for d in active_with_cat]

    headers = ["Category"] + project_names + ["Total"]
    ws.append(headers)
    _style_header(ws, 1, len(headers))

    cat_totals: dict[str, int] = defaultdict(int)
    for d in active_with_cat:
        for cat, cnt in d.category_breakdown.items():
            cat_totals[cat] += cnt

    sorted_cats = sorted(cat_totals, key=cat_totals.get, reverse=True)

    for row_idx, cat in enumerate(sorted_cats, 2):
        ws.cell(row=row_idx, column=1, value=cat)
        _style_data_cell(ws.cell(row=row_idx, column=1))
        for col_idx, d in enumerate(active_with_cat, 2):
            cnt = d.category_breakdown.get(cat, 0)
            cell = ws.cell(row=row_idx, column=col_idx, value=cnt)
            _style_data_cell(cell, is_num=True)
        cell_total = ws.cell(row=row_idx, column=len(headers), value=cat_totals[cat])
        _style_data_cell(cell_total, is_num=True)
        cell_total.font = Font(name="Calibri", bold=True, size=11)

    _auto_width(ws, min_width=12, max_width=50)
