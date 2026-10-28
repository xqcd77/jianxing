"""Generate ID/tag contracts from the shipped HTML, without a JS framework."""
import json
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1] / "bank_agent/static"
types = {"input": "HTMLInputElement", "textarea": "HTMLTextAreaElement", "select": "HTMLSelectElement",
         "button": "HTMLButtonElement", "dialog": "HTMLDialogElement", "form": "HTMLFormElement", "canvas": "HTMLCanvasElement"}
lines = ["// Generated from index.html; update with scripts/generate_dom_types.py.", "export interface Elements {"]
for match in re.finditer(r'<([a-z0-9-]+)\b[^>]*\bid="([^"]+)"', (root / "index.html").read_text(encoding="utf-8")):
    lines.append("  " + json.dumps(match[2]) + ": " + types.get(match[1], "HTMLElement") + ";")
(root / "dom-contracts.d.ts").write_text("\n".join([*lines, "}"]) + "\n", encoding="utf-8")
