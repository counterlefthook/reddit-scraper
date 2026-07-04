"""Generate examples/sample_urls.xlsx - a template for the URL list upload."""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

rows = [
    ["Reddit post URL", "Notes (optional - ignored by the scraper)"],
    ["https://www.reddit.com/r/AskReddit/comments/1abcde/example_thread/", "replace with real links"],
    ["https://redd.it/1fghij", "short links work too"],
    ["https://old.reddit.com/r/python/comments/1klmno/another_example/", "old.reddit links work"],
]

wb = Workbook()
ws = wb.active
ws.title = "URLs"
for row in rows:
    ws.append(row)
for cell in ws[1]:
    cell.font = Font(bold=True)
ws.column_dimensions["A"].width = 70
ws.column_dimensions["B"].width = 45

out = Path(__file__).parent / "sample_urls.xlsx"
wb.save(out)
print(f"wrote {out}")
