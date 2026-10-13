#!/usr/bin/env python3
"""Export a Markdown document to a styled PDF.

Renders through HTML and Chromium's print-to-PDF, which gives correct table
borders, page breaks and font embedding -- none of which a pure-Python PDF
writer does without a lot of work.

Usage
-----
    python scripts/export_pdf.py PROGRESS_REPORT.md
    python scripts/export_pdf.py PROGRESS_REPORT.md -o docs/report.pdf
    python scripts/export_pdf.py README.md --title "Technical Documentation"

The stylesheet is print-first: generous line height for readability on paper,
repeating table headers across page breaks, and colours chosen to survive a
grayscale print (status is never conveyed by hue alone -- every coloured cell
also carries its status as text).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Print-oriented stylesheet. Deliberately high-contrast and serif-free; a
#: report read on paper and on screen should not need two designs.
CSS = """
@page { size: A4; margin: 18mm 16mm; }

body {
  font-family: "DejaVu Sans", "Liberation Sans", Arial, sans-serif;
  font-size: 10.5pt;
  line-height: 1.55;
  color: #1a1a1a;
  margin: 0;
}

h1 {
  font-size: 20pt; margin: 0 0 2mm 0; padding-bottom: 2mm;
  border-bottom: 2.5px solid #1f3d5c; color: #1f3d5c;
}
h2 {
  font-size: 14pt; margin: 9mm 0 2.5mm 0; padding-bottom: 1.2mm;
  border-bottom: 1px solid #c8d0d8; color: #1f3d5c;
  /* Keep a heading with the content that follows it. */
  break-after: avoid; page-break-after: avoid;
}
h3 {
  font-size: 11.5pt; margin: 6mm 0 1.5mm 0; color: #2c4a68;
  break-after: avoid; page-break-after: avoid;
}

p { margin: 0 0 2.6mm 0; orphans: 2; widows: 2; }

hr { border: none; border-top: 1px solid #dde3e9; margin: 6mm 0; }

table {
  border-collapse: collapse; width: 100%; margin: 3mm 0 4mm 0;
  font-size: 9.2pt; break-inside: auto;
}
/* Repeat the header on every page a long table spans. */
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td { border: 1px solid #b8c2cc; padding: 1.6mm 2.2mm; text-align: left;
         vertical-align: top; }
th { background: #1f3d5c; color: #fff; font-weight: 600; }
tbody tr:nth-child(even) { background: #f4f7fa; }

code {
  font-family: "DejaVu Sans Mono", "Liberation Mono", monospace;
  font-size: 8.8pt; background: #eef2f6; padding: 0.3mm 1mm;
  border-radius: 2px;
}
pre {
  background: #f4f7fa; border-left: 3px solid #1f3d5c;
  padding: 2.5mm 3mm; overflow-x: auto; font-size: 8.6pt; line-height: 1.4;
  break-inside: avoid;
}
pre code { background: none; padding: 0; }

blockquote {
  margin: 3mm 0; padding: 2mm 4mm; border-left: 3px solid #b8860b;
  background: #fdf8ec; color: #3d3222;
}

strong { color: #0d1b2a; }

/* Status is always spelled out in the cell text as well as coloured, so the
   document survives a grayscale print. */
td strong { font-weight: 700; }

ul, ol { margin: 0 0 2.6mm 0; padding-left: 6mm; }
li { margin-bottom: 1mm; }
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head><body>
{body}
</body></html>
"""


def find_chromium() -> str | None:
    """Locate a Chromium-family browser."""
    override = shutil.which("CHROMIUM") or ""
    if override:
        return override
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def render_html(markdown_path: Path) -> str:
    """Convert a Markdown file to a standalone, styled HTML document."""
    import markdown

    text = markdown_path.read_text(encoding="utf-8")
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
        # Renders the trailing-two-space hard breaks that GFM honours. Without
        # it a block of "**Label:** value" lines collapses into a single
        # run-on paragraph, which is unreadable in the metadata header.
        break_on_newline=True,
    )
    return HTML_TEMPLATE.format(
        title=markdown_path.stem.replace("_", " ").title(), css=CSS, body=body,
    )


def export(markdown_path: Path, out_path: Path, title: str | None = None) -> Path:
    """Render ``markdown_path`` to a PDF at ``out_path``.

    Raises
    ------
    FileNotFoundError
        If the Markdown file does not exist.
    RuntimeError
        If no Chromium-family browser is available.
    """
    if not markdown_path.is_file():
        raise FileNotFoundError(f"no such markdown file: {markdown_path}")
    browser = find_chromium()
    if browser is None:
        raise RuntimeError(
            "no Chromium-family browser found; install chromium or use "
            "`pandoc -o out.pdf in.md` instead"
        )

    html = render_html(markdown_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "doc.html"
        html_path.write_text(html, encoding="utf-8")
        proc = subprocess.run(
            [browser, "--headless", "--disable-gpu", "--no-sandbox",
             "--no-first-run", "--no-default-browser-check",
             "--run-all-compositor-stages-before-draw",
             "--virtual-time-budget=8000",
             # Suppresses Chromium's default header/footer, which otherwise
             # stamps the current date, the page title and the source file://
             # URL onto every page -- the URL in particular leaks the build path
             # into a document meant to be handed in.
             "--no-pdf-header-footer",
             f"--print-to-pdf={out_path}",
             f"file://{html_path}"],
            capture_output=True, text=True, timeout=180, check=False,
        )
    if not out_path.is_file():
        raise RuntimeError(
            f"chromium produced no PDF (exit {proc.returncode})\n"
            f"{proc.stderr[-800:]}"
        )
    if title:
        print(f"title: {title}")
    return out_path


def main(argv=None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="markdown file to export")
    parser.add_argument("-o", "--out", default=None,
                        help="output PDF path (default: alongside the source)")
    parser.add_argument("--title", default=None,
                        help="document title (for the log line only)")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_absolute():
        source = ROOT / source
    out = Path(args.out) if args.out else source.with_suffix(".pdf")
    if not out.is_absolute():
        out = ROOT / out

    try:
        path = export(source, out, args.title)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    size_kb = path.stat().st_size / 1024
    print(f"wrote {path} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
