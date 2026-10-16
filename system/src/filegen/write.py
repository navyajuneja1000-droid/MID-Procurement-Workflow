"""
Write the file a bank can actually open.

Three things here are not cosmetic.

**Identifier columns are written as text.** A PAN is fine either way, but a
pincode starting with a zero, a twelve-digit account number and a four-digit
MCC are all numbers as far as a spreadsheet is concerned, and it will store
them as numbers: the zero goes, the account number becomes 6.95495E+11, and
the file the bank reads is not the file that was reviewed.

**Dropdowns are written as real validation.** The template is sent to the bank,
but it also goes to whoever reviews it, and a cell that refuses a wrong value
is worth more than a note asking people to be careful.

**The file name carries the batch.** Acquirer, date and lot, in that order, so
a folder of them sorts usefully and a reply naming a lot can be traced back to
exactly what was sent.
"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .fill import FilledFile
from .spec import Source

HEADER_FILL = PatternFill("solid", fgColor="EDF1F7")
OWNED_ELSEWHERE_FILL = PatternFill("solid", fgColor="FFF4E5")
HEADER_FONT = Font(bold=True, size=10)


def file_name(f: FilledFile, lot_no: str, date_stamp: str, part: int = 0) -> str:
    suffix = f"_part{part}" if part else ""
    return f"{date_stamp}_{f.acquirer_code}_lot{lot_no}{suffix}.{f.template.file_format}"


def write(f: FilledFile, out_dir: Path, lot_no: str, date_stamp: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / file_name(f, lot_no, date_stamp)
    if f.template.file_format == "csv":
        _write_csv(f, path)
    else:
        _write_xlsx(f, path)
    return path


def _write_csv(f: FilledFile, path: Path) -> None:
    sheet = f.template.primary
    headers = [c.header for c in sheet.columns]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in f.rows_for(sheet.name):
            writer.writerow([row.values.get(h, "") for h in headers])


def _write_xlsx(f: FilledFile, path: Path) -> None:
    wb = Workbook()
    wb.remove(wb.active)

    for sheet in f.template.sheets:
        ws = wb.create_sheet(sheet.name[:31])
        headers = [c.header for c in sheet.columns]

        for i, column in enumerate(sheet.columns, start=1):
            cell = ws.cell(row=1, column=i, value=column.header)
            cell.font = HEADER_FONT
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            # Columns another team owns are tinted, so a reviewer can see at a
            # glance that the blanks are waiting on someone else rather than
            # on a bug in the generator.
            cell.fill = (
                OWNED_ELSEWHERE_FILL
                if column.source in (Source.SALES_OWNED, Source.FINANCE_OWNED)
                else HEADER_FILL
            )
            ws.column_dimensions[get_column_letter(i)].width = min(
                max(12, len(column.header) + 2), 38
            )

        for r, row in enumerate(f.rows_for(sheet.name), start=2):
            for i, column in enumerate(sheet.columns, start=1):
                value = row.values.get(column.header, "")
                cell = ws.cell(row=r, column=i, value=value)
                if column.text_format:
                    # Both halves are needed: the format stops the display
                    # rounding, and writing the value as a string stops the
                    # stored value being a float in the first place.
                    cell.number_format = "@"

        n_rows = len(f.rows_for(sheet.name))
        for i, column in enumerate(sheet.columns, start=1):
            domain = f.template.domains.get(column.domain) if column.domain else None
            if domain is None or not domain.strict:
                continue
            letter = get_column_letter(i)
            dv = DataValidation(
                type="list",
                formula1='"' + ",".join(domain.values) + '"',
                allow_blank=True,
                showDropDown=False,
            )
            dv.error = f"Not one of this column's permitted values ({domain.name})."
            dv.errorTitle = "Value not allowed"
            ws.add_data_validation(dv)
            dv.add(f"{letter}2:{letter}{max(n_rows + 1, 200)}")

        ws.freeze_panes = "A2"

    # The dropdown reference sheet these forms carry. It is what a person
    # filling the file by hand reads, and it is generated from the same domains
    # the validation uses, so the sheet and the rules cannot drift apart.
    if f.template.domains:
        ref = wb.create_sheet("Drop down list")
        for i, (name, domain) in enumerate(sorted(f.template.domains.items()), start=1):
            head = ref.cell(row=1, column=i, value=name)
            head.font = HEADER_FONT
            head.fill = HEADER_FILL
            ref.column_dimensions[get_column_letter(i)].width = 26
            for r, value in enumerate(domain.values, start=2):
                ref.cell(row=r, column=i, value=value)

    wb.save(path)
