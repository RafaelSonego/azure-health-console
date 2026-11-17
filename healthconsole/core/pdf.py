"""PDF export with fpdf2: pure Python, so `pip install` is all it needs on Windows, macOS or Linux.
Built-in fonts cover Latin text only; set `pdf_font` to a .ttf for other scripts."""
from fpdf import FPDF
from fpdf.fonts import FontFace

from . import baseline
from .model import CRITICAL, HEALTHY, UNKNOWN, WARNING
from .report import STATUS_COLOURS, BOX_H, BOX_W, _fmt, layout, signal_detail, summary
from .report import title as report_title

PAGE_W, PAGE_H, MARGIN = 297, 210, 12
TEXT = (31, 35, 40)
MUTED = (87, 96, 106)
LINE = (140, 149, 159)
BORDER = (208, 215, 222)
MAX_GRAPH_SCALE = 0.3  # 190 x 46 layout units -> 57 x 14 mm boxes at most

# Built-in PDF fonts are Latin-1; these keep common symbols readable instead of "?".
_REPLACEMENTS = {"–": "-", "—": "-", "≥": ">=", "≤": "<=", "×": "x", "…": "...", "→": "->",
                 "‘": "'", "’": "'", "“": '"', "”": '"', "•": "-", " ": " "}


def _rgb(hex_colour):
    hex_colour = hex_colour.lstrip("#")
    return tuple(int(hex_colour[i:i + 2], 16) for i in (0, 2, 4))


class _Report(FPDF):
    def __init__(self, font_path=None):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(True, MARGIN)
        self.unicode = bool(font_path)
        if font_path:
            self.add_font("report", "", font_path)
            self.add_font("report", "B", font_path)
            self.family = "report"
        else:
            self.family = "helvetica"

    def clean(self, text):
        text = "" if text is None else str(text)
        if self.unicode:
            return text
        for old, new in _REPLACEMENTS.items():
            text = text.replace(old, new)
        return text.encode("latin-1", "replace").decode("latin-1")

    def font(self, size, bold=False, colour=TEXT):
        self.set_font(self.family, "B" if bold else "", size)
        self.set_text_color(*colour)

    def footer(self):
        self.set_y(-9)
        self.font(7, colour=MUTED)
        self.cell(0, 4, f"{self.page_no()} / {{nb}}", align="R")

    def pill(self, status, x=None, y=None):
        fill, ink = (_rgb(c) for c in STATUS_COLOURS.get(status, STATUS_COLOURS[UNKNOWN]))
        self.font(8, bold=True, colour=ink)
        width = self.get_string_width(status) + 5
        if x is not None:
            self.set_xy(x, y)
        self.set_fill_color(*fill)
        self.cell(width, 5, status, fill=True, align="C")


def _neutral(pdf):
    """Reset colours so the graph's per-status fills never leak into tables."""
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(*BORDER)
    pdf.set_line_width(0.2)


def _status_face(status):
    fill, ink = (_rgb(c) for c in STATUS_COLOURS.get(status, STATUS_COLOURS[UNKNOWN]))
    return FontFace(color=ink, fill_color=fill, emphasis="BOLD")


def _graph(pdf, snapshot):
    position, width, height = layout(snapshot)
    if not position:
        return
    avail_w = PAGE_W - 2 * MARGIN
    avail_h = PAGE_H - MARGIN - pdf.get_y() - 10
    if avail_h < 60:
        pdf.add_page()
        avail_h = PAGE_H - 2 * MARGIN - 10
    scale = min(avail_w / width, avail_h / height, MAX_GRAPH_SCALE)
    ox, oy = MARGIN, pdf.get_y() + 2

    def at(point):
        return ox + point[0] * scale, oy + point[1] * scale

    box_w, box_h = BOX_W * scale, BOX_H * scale
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    for edge in snapshot.edges:
        if edge.source not in position or edge.target not in position:
            continue
        (sx, sy), (tx, ty) = at(position[edge.source]), at(position[edge.target])
        x1, x2 = (sx + box_w, tx) if sx <= tx else (sx, tx + box_w)
        pdf.line(x1, sy + box_h / 2, x2, ty + box_h / 2)
    name_size = max(4.5, 7.5 * scale / MAX_GRAPH_SCALE)
    for node in snapshot.nodes:
        x, y = at(position[node.id])
        fill, ink = (_rgb(c) for c in STATUS_COLOURS.get(node.status, STATUS_COLOURS[UNKNOWN]))
        pdf.set_fill_color(*fill)
        pdf.set_draw_color(*ink)
        pdf.set_line_width(0.6 if node.direction == "root" else 0.25)
        pdf.rect(x, y, box_w, box_h, style="DF", round_corners=True, corner_radius=1.2)
        name = node.name if len(node.name) <= 30 else node.name[:29] + "..."
        pdf.font(name_size, bold=True)
        pdf.text(x + 1.5, y + box_h * 0.42, pdf.clean(name))
        pdf.font(name_size * 0.8, colour=MUTED)
        pdf.text(x + 1.5, y + box_h * 0.8, pdf.clean(f"{node.display_type} - {node.status}"))
    _neutral(pdf)
    pdf.set_y(oy + height * scale + 4)


def render(snapshot, font_path=None):
    pdf = _Report(font_path)
    pdf.alias_nb_pages()
    pdf.add_page()
    counts = snapshot.counts()

    pdf.font(16, bold=True)
    pdf.cell(0, 8, pdf.clean(f"Health snapshot: {report_title(snapshot)}"), new_x="LMARGIN", new_y="NEXT")
    pdf.pill(snapshot.status)
    pdf.font(8, colour=MUTED)
    pdf.cell(0, 5, pdf.clean(
        f"   {len(snapshot.nodes)} resources: {counts[CRITICAL]} critical, {counts[WARNING]} warning, "
        f"{counts[HEALTHY]} healthy, {counts[UNKNOWN]} unknown - taken {snapshot.taken_at} by {snapshot.taken_by}"
        f" - depth {snapshot.depth} - {snapshot.arm_calls} Azure calls, read-only"), new_x="LMARGIN", new_y="NEXT")

    if snapshot.service_health:
        pdf.ln(2)
        pdf.font(11, bold=True)
        pdf.cell(0, 6, "Active Service Health events", new_x="LMARGIN", new_y="NEXT")
        pdf.font(8)
        for event in snapshot.service_health:
            many = f", {len(event['tracking_ids'])} events" if len(event.get("tracking_ids", [])) > 1 else ""
            pdf.multi_cell(0, 4, pdf.clean(f"- {event['type']}: {event['title']}{many} ({', '.join(event['services'])}; "
                                           f"{', '.join(event['regions']) or 'all regions'})"),
                           new_x="LMARGIN", new_y="NEXT")

    if snapshot.baseline.get("movers"):
        b = snapshot.baseline
        pdf.ln(2)
        pdf.font(11, bold=True)
        pdf.cell(0, 6, f"Against last week (same {b['window_minutes']} minutes, a week earlier)", new_x="LMARGIN", new_y="NEXT")
        pdf.font(7.5)
        _neutral(pdf)
        with pdf.table(col_widths=(60, 80, 40, 40, 53), line_height=4, text_align="LEFT",
                       headings_style=FontFace(emphasis="BOLD", fill_color=(246, 248, 250))) as table:
            table.row(["Resource", "Metric", "Now", "Last week", "Change"])
            for m in b["movers"]:
                table.row([pdf.clean(m["name"]), pdf.clean(m["signal"]), pdf.clean(f"{_fmt(m['value'])} {m['unit']}".strip()),
                           pdf.clean(_fmt(m["baseline"])), pdf.clean(baseline.describe(m))])

    pdf.ln(2)
    pdf.font(11, bold=True)
    pdf.cell(0, 6, "Dependency graph", new_x="LMARGIN", new_y="NEXT")
    _graph(pdf, snapshot)

    pdf.font(11, bold=True)
    pdf.cell(0, 6, "Resources", new_x="LMARGIN", new_y="NEXT")
    pdf.font(7.5)
    _neutral(pdf)
    with pdf.table(col_widths=(16, 52, 32, 52, 121), line_height=4, text_align="LEFT",
                   headings_style=FontFace(emphasis="BOLD", fill_color=(246, 248, 250))) as table:
        table.row(["Status", "Name", "Type", "Resource group", "Why"])
        for node in snapshot.nodes:
            row = table.row()
            row.cell(node.status, style=_status_face(node.status))
            for value in (node.name, node.display_type, node.resource_group, summary(node)):
                row.cell(pdf.clean(value))

    for node in snapshot.nodes:
        if pdf.get_y() > PAGE_H - 50:
            pdf.add_page()
        pdf.ln(3)
        pdf.font(10, bold=True)
        pdf.cell(pdf.get_string_width(pdf.clean(node.name)) + 3, 6, pdf.clean(node.name))
        pdf.pill(node.status)
        _neutral(pdf)
        pdf.ln(6)
        pdf.font(6.5, colour=MUTED)
        pdf.multi_cell(0, 3.2, pdf.clean(f"{node.display_type} - {node.id}"), new_x="LMARGIN", new_y="NEXT")
        if node.signals:
            pdf.font(7)
            _neutral(pdf)
            with pdf.table(col_widths=(48, 20, 16, 26, 30, 18, 115), line_height=3.6, text_align="LEFT",
                           headings_style=FontFace(emphasis="BOLD", fill_color=(246, 248, 250))) as table:
                table.row(["Signal", "Source", "Status", "Value", "Threshold", "Last week", "Detail"])
                for s in node.signals:
                    row = table.row()
                    row.cell(pdf.clean(s.name))
                    row.cell(pdf.clean(s.source))
                    row.cell(s.status, style=_status_face(s.status))
                    row.cell(pdf.clean(f"{_fmt(s.value)} {s.unit}".strip() if s.value is not None else "-"))
                    row.cell(pdf.clean(s.threshold))
                    row.cell(pdf.clean(_fmt(s.baseline)))
                    row.cell(pdf.clean(signal_detail(s)))
        if node.changes:
            pdf.font(7.5, bold=True)
            pdf.cell(0, 5, "Recent changes", new_x="LMARGIN", new_y="NEXT")
            pdf.font(7)
            for change in node.changes[:10]:
                fields = f" ({', '.join(change['fields'])})" if change["fields"] else ""
                pdf.multi_cell(0, 3.6, pdf.clean(f"- {change['at']}: {change['type']} by {change['by'] or 'unknown'}{fields}"),
                               new_x="LMARGIN", new_y="NEXT")

    if snapshot.warnings:
        pdf.ln(3)
        pdf.font(11, bold=True)
        pdf.cell(0, 6, "Notes", new_x="LMARGIN", new_y="NEXT")
        pdf.font(8)
        for warning in snapshot.warnings:
            pdf.multi_cell(0, 4, pdf.clean(f"- {warning}"), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
