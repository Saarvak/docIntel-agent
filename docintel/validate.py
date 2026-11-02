"""Business-rule validation. Each rule returns a check with a status and, when it fails, an exception queue."""

from __future__ import annotations

from datetime import date
from typing import Any

from .extract import PLACEHOLDER_RE, Extraction
from .text import approx_equal

# Valid GST state codes (01-38 plus 97 and 99).
GST_STATE_CODES = {f"{n:02d}" for n in range(1, 39)} | {"97", "99"}

QUEUES = {
    "invoice": "ap_exceptions",
    "purchase-order": "procurement_exceptions",
    "contract": "legal_review",
    "general": "manual_review",
}


def _check(rule: str, status: str, message: str, severity: str = "error", fields: list[str] | None = None) -> dict[str, Any]:
    return {"rule": rule, "status": status, "severity": severity if status != "pass" else "info", "message": message, "fields": fields or []}


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _required(ex: Extraction, keys: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for key, label in keys.items():
        if ex.get(key) in (None, ""):
            out.append(_check(f"required.{key}", "fail", f"{label} was not found in the document.", fields=[key]))
        else:
            out.append(_check(f"required.{key}", "pass", f"{label} present."))
    return out


def _placeholders(ex: Extraction) -> dict[str, Any]:
    hits = [k for k, f in ex.fields.items() if isinstance(f.value, str) and PLACEHOLDER_RE.search(f.value)]
    hits += [f"line_items[{i}]" for i, it in enumerate(ex.line_items) if PLACEHOLDER_RE.search(it["description"])]
    if hits:
        return _check("quality.placeholders", "warn", "Template placeholder text found (e.g. [Company Name]); this may be an unfilled template.", "warning", hits)
    return _check("quality.placeholders", "pass", "No template placeholders detected.")


def _line_math(ex: Extraction) -> list[dict[str, Any]]:
    checks = []
    bad = []
    for i, item in enumerate(ex.line_items):
        q, p, a = item["quantity"], item["unit_price"], item["amount"]
        if q is not None and p is not None and a is not None and not approx_equal(q * p, a):
            bad.append(f"line {i + 1} '{item['description'][:30]}': {q:g} x {_money(p)} = {_money(q * p)}, document says {_money(a)}")
    if not ex.line_items:
        checks.append(_check("line_items.present", "fail", "No line items could be extracted.", fields=["line_items"]))
        return checks
    checks.append(_check("line_items.present", "pass", f"{len(ex.line_items)} line item(s) extracted."))
    if bad:
        checks.append(_check("line_items.qty_x_price", "fail", "Quantity x unit price does not match the line amount: " + "; ".join(bad), fields=["line_items"]))
    else:
        checks.append(_check("line_items.qty_x_price", "pass", "Quantity x unit price matches line amounts."))
    return checks


def _totals(ex: Extraction) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    amounts = [i["amount"] for i in ex.line_items if i["amount"] is not None]
    line_sum = round(sum(amounts), 2) if amounts else None
    subtotal, total = ex.get("subtotal"), ex.get("total_amount")
    tax = ex.get("tax_amount") or 0.0
    discount = ex.get("discount") or 0.0
    shipping = ex.get("shipping") or 0.0

    if subtotal is not None and line_sum is not None:
        if approx_equal(line_sum, subtotal):
            checks.append(_check("totals.lines_vs_subtotal", "pass", f"Line items add up to the subtotal ({_money(subtotal)})."))
        else:
            checks.append(_check("totals.lines_vs_subtotal", "fail", f"Line items add up to {_money(line_sum)} but the subtotal is {_money(subtotal)}.", fields=["subtotal", "line_items"]))

    if subtotal is not None and ex.get("tax_rate") and ex.get("tax_amount") is not None:
        rate = ex.get("tax_rate") / 100
        candidates = [subtotal * rate, (subtotal - discount) * rate, (subtotal - discount + shipping) * rate]
        if any(approx_equal(c, tax) for c in candidates):
            checks.append(_check("totals.tax_rate", "pass", f"Tax {_money(tax)} is consistent with the {ex.get('tax_rate'):g}% rate."))
        else:
            checks.append(_check("totals.tax_rate", "fail", f"Tax {_money(tax)} does not match {ex.get('tax_rate'):g}% of the taxable amount (expected about {_money(candidates[0])}).", fields=["tax_amount", "tax_rate"]))

    base = subtotal if subtotal is not None else line_sum
    if total is None:
        if base is not None:
            computed = base - discount + shipping + tax
            checks.append(_check("totals.total_present", "warn", f"No document total was found. Computed from the parts: {_money(computed)}.", "warning", ["total_amount"]))
        else:
            checks.append(_check("required.total_amount", "fail", "Total amount was not found in the document.", fields=["total_amount"]))
        return checks

    if subtotal is not None:
        expected = subtotal - discount + shipping + tax
        if approx_equal(expected, total):
            checks.append(_check("totals.arithmetic", "pass", f"Subtotal, adjustments and tax add up to the total ({_money(total)})."))
        else:
            checks.append(_check("totals.arithmetic", "fail", f"Subtotal {_money(subtotal)} - discount {_money(discount)} + shipping {_money(shipping)} + tax {_money(tax)} = {_money(expected)}, but the total is {_money(total)}.", fields=["total_amount"]))
    elif line_sum is not None:
        if approx_equal(line_sum, total) or approx_equal(line_sum + tax, total) or approx_equal(line_sum - discount + shipping + tax, total):
            checks.append(_check("totals.lines_vs_total", "pass", f"Line items reconcile with the total ({_money(total)})."))
        else:
            checks.append(_check("totals.lines_vs_total", "fail", f"Line items add up to {_money(line_sum)} but the document total is {_money(total)}.", fields=["total_amount", "line_items"]))
    return checks


def _date_order(first: str | None, second: str | None, rule: str, first_label: str, second_label: str, terms_days: int | None) -> dict[str, Any] | None:
    if not (first and second):
        return None
    a, b = date.fromisoformat(first), date.fromisoformat(second)
    if b < a:
        return _check(rule, "fail", f"{second_label} ({second}) is before {first_label} ({first}).", fields=[rule])
    if b == a and terms_days:
        return _check(rule, "warn", f"{second_label} equals {first_label} ({first}) although the payment terms allow {terms_days} days.", "warning", [rule])
    return _check(rule, "pass", f"{second_label} is on or after {first_label}.")


def _gst(ex: Extraction) -> list[dict[str, Any]]:
    gstin = ex.get("gstin")
    if not gstin:
        return []
    checks = []
    if gstin[:2] in GST_STATE_CODES:
        checks.append(_check("tax_id.gstin_state", "pass", f"GSTIN {gstin} has a valid state code ({gstin[:2]})."))
    else:
        checks.append(_check("tax_id.gstin_state", "fail", f"GSTIN {gstin} has an invalid state code ({gstin[:2]}).", fields=["gstin"]))
    pan = ex.get("pan")
    if pan:
        status = "pass" if gstin[2:12] == pan else "fail"
        checks.append(_check("tax_id.gstin_pan", status, "PAN embedded in the GSTIN matches the PAN on the document." if status == "pass" else f"PAN {pan} does not match the PAN inside GSTIN {gstin}.", fields=["gstin", "pan"]))
    return checks


def _duplicate(ex: Extraction, registry: dict[str, str] | None, document_id: str) -> dict[str, Any] | None:
    if registry is None:
        return None
    if ex.document_type == "invoice":
        key = (ex.get("vendor_name"), ex.get("invoice_number"), ex.get("total_amount"))
    elif ex.document_type == "purchase-order":
        key = (ex.get("vendor_name"), ex.get("po_number"), ex.get("total_amount"))
    else:
        return None
    if not key[1]:
        return None
    joined = "|".join(str(k) for k in key)
    previous = registry.get(joined)
    registry.setdefault(joined, document_id)
    if previous and previous != document_id:
        return _check("duplicate.document", "fail", f"A document with the same vendor, number and amount was already processed ({previous}).", fields=["invoice_number", "po_number"])
    return _check("duplicate.document", "pass", "No duplicate found.")


def validate(ex: Extraction, review_threshold: float, registry: dict[str, str] | None = None, document_id: str = "") -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    if ex.document_type == "invoice":
        checks += _required(ex, {"invoice_number": "Invoice number", "invoice_date": "Invoice date", "vendor_name": "Vendor name"})
        if ex.get("currency") is None:
            checks.append(_check("required.currency", "warn", "Currency could not be determined.", "warning", ["currency"]))
        if c := _date_order(ex.get("invoice_date"), ex.get("due_date"), "due_date", "the invoice date", "The due date", ex.get("payment_terms_days")):
            checks.append(c)
        checks += _line_math(ex)
        checks += _totals(ex)
        checks += _gst(ex)
        if ex.get("po_number") is None:
            checks.append(_check("match.po_reference", "warn", "No PO reference on the invoice, so PO matching was skipped.", "warning", ["po_number"]))

    elif ex.document_type == "purchase-order":
        checks += _required(ex, {"po_number": "PO number", "vendor_name": "Vendor", "order_date": "Order date"})
        if c := _date_order(ex.get("order_date"), ex.get("delivery_date"), "delivery_date", "the order date", "The delivery date", None):
            checks.append(c)
        checks += _line_math(ex)
        checks += _totals(ex)

    elif ex.document_type == "contract":
        parties = ex.get("parties") or []
        checks.append(_check("required.parties", "pass" if len(parties) >= 2 else "fail", f"{len(parties)} part{'y' if len(parties) == 1 else 'ies'} identified." if parties else "Contracting parties were not found.", fields=["parties"]))
        checks += _required(ex, {"effective_date": "Effective date", "governing_law": "Governing law"})
        clauses = ex.extras.get("clauses", {})
        for name in ("termination", "confidentiality", "liability", "payment"):
            present = clauses.get(name)
            checks.append(_check(f"clause.{name}", "pass" if present else "fail", f"{name.replace('_', ' ').title()} clause {'found' if present else 'is missing'}.", fields=[name]))
        if ex.get("auto_renewal"):
            notice = ex.get("renewal_notice_days")
            checks.append(_check("renewal.owner", "warn", f"Auto-renewal clause (notice {notice} days). Assign an owner and a reminder before the notice deadline." if notice else "Auto-renewal clause without a clear notice period.", "warning", ["auto_renewal"]))
        if not ex.get("liability_cap"):
            checks.append(_check("clause.liability_cap", "warn", "No liability cap detected.", "warning", ["liability_cap"]))
        if (pct := ex.get("late_interest_pct_month")) and pct > 1.5:
            checks.append(_check("policy.late_interest", "warn", f"Late-payment interest of {pct:g}% per month exceeds the 1.5% policy limit.", "warning", ["late_interest_pct_month"]))

    checks.append(_placeholders(ex))

    low = [k for k, f in ex.fields.items() if isinstance(f.value, (str, int, float)) and not isinstance(f.value, bool) and f.confidence < review_threshold]
    if low:
        checks.append(_check("confidence.low", "warn", f"Low confidence on: {', '.join(low)}.", "warning", low))

    if c := _duplicate(ex, registry, document_id):
        checks.append(c)
    return checks


def summarize(checks: list[dict[str, Any]], document_type: str) -> dict[str, Any]:
    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    status = "needs_review" if failed else "passed_with_warnings" if warned else "passed"
    queue = QUEUES.get(document_type, "manual_review")
    return {
        "status": status,
        "passed": sum(c["status"] == "pass" for c in checks),
        "failed": len(failed),
        "warnings": len(warned),
        "straight_through": status == "passed",
        "exceptions": [{"queue": queue, "rule": c["rule"], "message": c["message"]} for c in failed],
    }
