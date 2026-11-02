"""Human-readable (Markdown) report for chat replies and the CLI."""

from __future__ import annotations

from typing import Any

STATUS_LABEL = {
    "passed": "PASSED - straight-through, no review needed",
    "passed_with_warnings": "PASSED WITH WARNINGS",
    "needs_review": "NEEDS HUMAN REVIEW",
}
ICON = {"pass": "[ok]", "warn": "[warn]", "fail": "[FAIL]"}


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, list):
        return "; ".join(f"{p.get('role', 'Party')}: {p['name']}" if isinstance(p, dict) else str(p) for p in v)
    return str(v)


def format_report(result: dict[str, Any]) -> str:
    v = result["validation"]
    cls = result["classification"]
    out = [
        f"## {result['filename']}",
        f"**Type:** {result['document_type']} ({cls['confidence']:.0%} confidence)  |  **Result:** {STATUS_LABEL[v['status']]}",
        "",
        "### Extracted fields",
    ]
    for key, f in result["fields"].items():
        out.append(f"- **{key}**: {_value(f['value'])}  _(conf {f['confidence']:.2f})_")

    if result["line_items"]:
        out += ["", "### Line items", "| # | Description | Qty | Unit price | Amount |", "|---|---|---|---|---|"]
        for i, item in enumerate(result["line_items"], 1):
            qty = "" if item["quantity"] is None else f"{item['quantity']:g}"
            money = ["" if item[k] is None else f"{item[k]:,.2f}" for k in ("unit_price", "amount")]
            out.append(f"| {i} | {item['description'][:48]} | {qty} | {money[0]} | {money[1]} |")

    out += ["", f"### Validation ({v['passed']} passed, {v['warnings']} warnings, {v['failed']} failed)"]
    for c in sorted(v["checks"], key=lambda c: {"fail": 0, "warn": 1, "pass": 2}[c["status"]]):
        if c["status"] != "pass":
            out.append(f"- {ICON[c['status']]} `{c['rule']}`: {c['message']}")
    if v["failed"] == 0 and v["warnings"] == 0:
        out.append("- All business rules passed.")

    if v["exceptions"]:
        queue = v["exceptions"][0]["queue"]
        out += ["", f"### Exception queue: `{queue}`", f"{len(v['exceptions'])} exception(s) routed for human review."]
    return "\n".join(out)
