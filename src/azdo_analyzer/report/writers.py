"""Excel, CSV and HTML writers."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from ..config.models import ExcelConfig
from ..util import write_text
from .tables import SHEET_IDS, Table

_ILLEGAL = dict.fromkeys(range(32), None)
del _ILLEGAL[9], _ILLEGAL[10], _ILLEGAL[13]
_FORMULA = ("=", "+", "-", "@")


def _safe(value: Any) -> Any:
    """Strip control characters and neutralise formula-looking cells (payloads are untrusted)."""
    if not isinstance(value, str):
        return value
    text = value.translate(_ILLEGAL)
    if text.startswith(_FORMULA):
        text = "'" + text
    return text[:32000]


def write_csv(tables: dict[str, Table], directory: Path) -> list[Path]:
    paths = []
    for table_id in SHEET_IDS:
        table = tables[table_id]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(table.columns)
        for row in table.rows:
            writer.writerow(["" if cell is None else _safe(cell) for cell in row])
        path = directory / f"{table_id}.csv"
        write_text(path, buffer.getvalue())
        paths.append(path)
    return paths


def write_excel(tables: dict[str, Table], path: Path, options: ExcelConfig) -> Path:
    wanted = SHEET_IDS if options.sheets == "all" else [s for s in SHEET_IDS if s in options.sheets]
    workbook = Workbook()
    workbook.remove(workbook.active)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="305496")
    for table_id in wanted:
        table = tables[table_id]
        sheet = workbook.create_sheet(table_id[:31])
        sheet.append(table.columns)
        for cell in sheet[1]:
            cell.font, cell.fill = header_font, header_fill
        for row in table.rows:
            sheet.append([_safe(cell) for cell in row])
        for index, column in enumerate(table.columns, start=1):
            longest = max([len(column), *[len(str(row[index - 1] or "")) for row in table.rows[:200]]])
            sheet.column_dimensions[get_column_letter(index)].width = min(60, max(10, longest + 2))
        if options.freeze_header:
            sheet.freeze_panes = "A2"
        if options.autofilter and table.columns:
            sheet.auto_filter.ref = f"A1:{get_column_letter(len(table.columns))}{max(1, len(table.rows) + 1)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AZDO pipeline estate - migration summary</title>
<style>
  :root { --bg:#ffffff; --fg:#1f2933; --muted:#5f6b7a; --line:#d9dee5; --head:#f1f4f8; --accent:#305496; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14181d; --fg:#e4e8ee; --muted:#9aa5b1; --line:#2c343d; --head:#1e252d; --accent:#8fb0e8; }
  }
  body { background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; margin:0; }
  main { max-width:1100px; margin:0 auto; padding:24px 16px 64px; }
  h1 { font-size:1.6rem; margin:0 0 4px; }
  h2 { font-size:1.15rem; margin:32px 0 8px; color:var(--accent); }
  p.note { color:var(--muted); margin:0 0 16px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:0 32px; }
  .scroll { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; margin:0 0 8px; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { background:var(--head); font-weight:600; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  ul { margin:0; padding-left:20px; }
</style>
</head>
<body>
<main>
<h1>Azure DevOps pipeline estate - migration summary</h1>
<p class="note">Data as of {{ d.as_of or "unknown" }}. Read-only analysis; nothing in Azure DevOps was changed.</p>

<h2>Estate totals</h2>
<div class="scroll"><table><tr><th>Metric</th><th>Value</th></tr>
{% for k, v in d.totals %}<tr><td>{{ k }}</td><td class="num">{{ v }}</td></tr>{% endfor %}</table></div>

<div class="grid">
{% for title, head, rows in pairs %}
<section><h2>{{ title }}</h2>
{% if rows %}<div class="scroll"><table><tr><th>{{ head }}</th><th>Pipelines</th></tr>
{% for k, v in rows %}<tr><td>{{ k }}</td><td class="num">{{ v }}</td></tr>{% endfor %}</table></div>
{% else %}<p class="note">None.</p>{% endif %}</section>
{% endfor %}
</div>

<h2>Top unmapped tasks</h2>
{% if d.unmapped %}<div class="scroll"><table><tr><th>Task</th><th>Major</th><th>Used by</th><th>Origin</th></tr>
{% for name, major, count, origin in d.unmapped %}<tr><td>{{ name }}</td><td>{{ major }}</td><td class="num">{{ count }}</td><td>{{ origin }}</td></tr>{% endfor %}</table></div>
{% else %}<p class="note">Every task in use has a mapping.</p>{% endif %}

<h2>Risks and blockers</h2>
{% if risks %}<ul>{% for count, text in risks %}<li><strong>{{ count }}</strong> {{ text }}</li>{% endfor %}</ul>
{% else %}<p class="note">None found.</p>{% endif %}

<h2>Suggested migration waves</h2>
<div class="scroll"><table><tr><th>Wave</th><th>Pipelines</th><th>Members</th></tr>
{% for name, count, members in d.waves %}<tr><td>{{ name }}</td><td class="num">{{ count }}</td><td>{{ members }}</td></tr>{% endfor %}</table></div>
</main>
</body>
</html>
"""


def render_html(data: dict[str, Any]) -> str:
    env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))
    pairs = [
        ("Active vs inactive", "Status", data["status"]),
        ("Classic vs YAML", "Type", data["kinds"]),
        ("Stack distribution", "Stack", data["stacks"]),
        ("Deploy targets", "Target", data["targets"]),
        ("Effort by band", "Band", data["effort"]),
        ("Template gaps by stack", "Stack", data["gaps"]),
    ]
    risks = []
    for item in data["risks"]:
        count, _, text = item.partition("** ")
        risks.append((count.strip("*"), text))
    return env.from_string(_HTML).render(d=data, pairs=pairs, risks=risks)
