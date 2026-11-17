"""Exports for the table pages (Secrets, Network): Markdown, CSV and PDF from one plain description:
{"title", "subtitle", "sections": [{"heading", "columns", "rows", "status_column"}]}."""
import csv
import io
import re
from datetime import datetime, timezone

from fpdf.fonts import FontFace

from .pdf import MARGIN, MUTED, PAGE_W, _neutral, _Report, _status_face
from .report import STATUS_ICONS, _cell

FORMATS = ("md", "csv", "pdf")


def filename(doc, fmt):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", doc["title"]).strip("-")
    return f"{stem}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}.{fmt}"


def render(doc, fmt, font_path=None):
    if fmt == "md":
        return to_markdown(doc).encode("utf-8")
    if fmt == "csv":
        return to_csv(doc).encode("utf-8-sig")  # the BOM makes Excel read UTF-8
    if fmt == "pdf":
        return to_pdf(doc, font_path)
    raise ValueError(f"Unknown format '{fmt}'; choose one of {FORMATS}.")


def to_markdown(doc):
    lines = [f"# {doc['title']}", "", doc.get("subtitle", ""), ""]
    for section in doc["sections"]:
        lines += [f"## {section['heading']}", ""]
        if section.get("note"):
            lines += [section["note"], ""]
        if not section["rows"]:
            lines += ["Nothing to show.", ""]
            continue
        status_col = section.get("status_column")
        icons = {**STATUS_ICONS, **section.get("icons", {})}
        lines.append("| " + " | ".join(section["columns"]) + " |")
        lines.append("|" + "---|" * len(section["columns"]))
        for row in section["rows"]:
            cells = [f"{icons.get(v, '')} {v}".strip() if i == status_col else _cell(v) for i, v in enumerate(row)]
            lines.append("| " + " | ".join(str(c) for c in cells) + " |")
        lines.append("")
    return "\n".join(lines)


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
PLAIN_NUMBER = re.compile(r"^-?[\d,]*\.?\d+%?$")


def csv_cell(value):
    """A cell a spreadsheet can't run: text starting like a formula (e.g. a resource named "=HYPERLINK(...)") gets a
    leading ' so it's shown, not evaluated. Numbers, including negative ones, stay numbers."""
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip(" ").startswith(FORMULA_START) and not PLAIN_NUMBER.match(value.strip()):
        return "'" + value
    return value


def to_csv(doc):
    """The first section only: CSV is one table."""
    section = doc["sections"][0]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([csv_cell(c) for c in section["columns"]])
    writer.writerows([csv_cell(v) for v in row] for row in section["rows"])
    return out.getvalue()


def to_pdf(doc, font_path=None):
    pdf = _Report(font_path)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.font(16, bold=True)
    pdf.cell(0, 8, pdf.clean(doc["title"]), new_x="LMARGIN", new_y="NEXT")
    pdf.font(8, colour=MUTED)
    pdf.multi_cell(0, 4, pdf.clean(doc.get("subtitle", "")), new_x="LMARGIN", new_y="NEXT")
    width = PAGE_W - 2 * MARGIN
    for section in doc["sections"]:
        pdf.ln(3)
        pdf.font(11, bold=True)
        pdf.cell(0, 6, pdf.clean(section["heading"]), new_x="LMARGIN", new_y="NEXT")
        if section.get("note"):
            pdf.font(7.5, colour=MUTED)
            pdf.multi_cell(0, 3.6, pdf.clean(section["note"]), new_x="LMARGIN", new_y="NEXT")
        if not section["rows"]:
            pdf.font(8)
            pdf.cell(0, 5, "Nothing to show.", new_x="LMARGIN", new_y="NEXT")
            continue
        weights = section.get("widths") or [1] * len(section["columns"])
        widths = [width * w / sum(weights) for w in weights]
        status_col = section.get("status_column")
        pdf.font(7)
        _neutral(pdf)
        with pdf.table(col_widths=widths, line_height=3.8, text_align="LEFT",
                       headings_style=FontFace(emphasis="BOLD", fill_color=(246, 248, 250))) as table:
            table.row([pdf.clean(c) for c in section["columns"]])
            for values in section["rows"]:
                row = table.row()
                for index, value in enumerate(values):
                    if index == status_col:
                        row.cell(str(value), style=_status_face(section.get("tones", {}).get(value, value)))
                    else:
                        row.cell(pdf.clean("" if value is None else value))
    return bytes(pdf.output())
