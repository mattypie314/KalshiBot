#!/usr/bin/env python3
"""Render docs/KalshiBot-operating-manual.md to PDF. Not used by the bot.

Needs fpdf2 and DejaVu TTF at /usr/share/fonts/truetype/dejavu.
"""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "KalshiBot-operating-manual.md"
DEST = ROOT / "docs" / "KalshiBot-operating-manual.pdf"
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


class ManualPDF(FPDF):
    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("DejaVu", size=9)
        self.set_text_color(90, 90, 90)
        self.set_x(self.l_margin)
        self.cell(0, 8, "KalshiBot operating manual  ·  /home/KalshiBot", align="L")
        self.ln(10)
        self.set_text_color(0, 0, 0)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("DejaVu", size=9)
        self.set_text_color(90, 90, 90)
        self.cell(0, 8, str(self.page_no()), align="C")
        self.set_text_color(0, 0, 0)


def _cells(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def build() -> Path:
    pdf = ManualPDF(format="Letter", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font("DejaVu", "", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
    pdf.add_font("DejaVuMono", "", str(FONT_DIR / "DejaVuSansMono.ttf"))
    pdf.add_page()
    pdf.set_margins(18, 16, 18)

    usable = pdf.w - pdf.l_margin - pdf.r_margin

    def write(text: str, *, size: float = 10, bold: bool = False, indent: float = 0, leading: float = 5.4) -> None:
        pdf.set_font("DejaVu", "B" if bold else "", size)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(usable - indent, leading, text)

    in_code = False
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        header, *rest = table_rows
        rows = [r for r in rest if not all(set(c) <= set("-: ") for c in r)]
        n = max(len(header), 1)
        col_w = usable / n
        pdf.set_font("DejaVu", "B", 7.5)
        pdf.set_x(pdf.l_margin)
        for cell in header:
            pdf.cell(col_w, 6, cell[:48], border=1)
        pdf.ln()
        pdf.set_font("DejaVu", "", 7.5)
        for row in rows:
            padded = list(row) + [""] * n
            if pdf.get_y() > pdf.h - 28:
                pdf.add_page()
            pdf.set_x(pdf.l_margin)
            for cell in padded[:n]:
                pdf.cell(col_w, 6, cell[:48], border=1)
            pdf.ln()
        pdf.ln(2)
        table_rows = []

    for raw in SRC.read_text().splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                in_code = False
                pdf.ln(2)
            else:
                flush_table()
                in_code = True
            continue
        if in_code:
            pdf.set_font("DejaVuMono", size=8)
            pdf.set_fill_color(245, 245, 245)
            pdf.set_x(pdf.l_margin)
            pdf.cell(usable, 5, (line or " ")[:110], fill=True)
            pdf.ln()
            continue
        if line.startswith("|"):
            table_rows.append(_cells(line))
            continue
        flush_table()
        if not line:
            pdf.ln(1.5)
            continue
        if line.startswith("# "):
            write(line[2:].strip(), size=20, bold=True, leading=9)
            pdf.ln(2)
            continue
        if line.startswith("## "):
            pdf.ln(2)
            write(line[3:].strip(), size=13, bold=True, leading=7)
            pdf.ln(1)
            continue
        if line.startswith("- "):
            write("•  " + line[2:].strip(), indent=4)
            continue
        if line[:2].rstrip(".").isdigit() and line[1:3] in {". ", ") "}:
            write(line.strip(), indent=4)
            continue
        write(line)

    flush_table()
    DEST.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(DEST))
    return DEST


if __name__ == "__main__":
    print(build())
