"""Rule-based classification and field extraction over MinerU Markdown.

Output is a canonical `Extraction` per document type, so validation never depends on how
the values were found (rules here, or gap-filling by an LLM in `llm.py`).
"""

from __future__ import annotations

import re
from typing import Any

try:
    from .text import (
        detect_currency,
        extract_tables,
        non_empty_lines,
        parse_amount,
        parse_date,
        parse_percent,
        to_plain_text,
    )
except ImportError:
    from text import (
        detect_currency,
        extract_tables,
        non_empty_lines,
        parse_amount,
        parse_date,
        parse_percent,
        to_plain_text,
    )

DOC_TYPES = ("invoice", "purchase-order", "contract", "general")
TYPE_ALIASES = {
    "invoice": "invoice",
    "tax invoice": "invoice",
    "tax-invoice": "invoice",
    "po": "purchase-order",
    "purchase order": "purchase-order",
    "purchase-order": "purchase-order",
    "contract": "contract",
    "agreement": "contract",
    "msa": "contract",
    "general": "general",
    "auto": "auto",
}


def parse_requested_type(text: str, ignore: list[str] | tuple[str, ...] = ()) -> str | None:
    """Return a DOC_TYPES value, 'auto', or None if the user did not name a type.

    Sample filenames are stripped first so `sample invoice-sliced` does not count as invoice.
    """
    cleaned = text or ""
    for token in ignore:
        cleaned = re.sub(re.escape(token), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"\b(sample|samples|json)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    for alias in sorted(TYPE_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", cleaned):
            return TYPE_ALIASES[alias]
    return None

GSTIN_RE = re.compile(r"\b(\d{2})([A-Z]{5}\d{4}[A-Z])([1-9A-Z])Z([0-9A-Z])\b")
PAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])")
AMOUNT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9.])[₹$€£]?\s?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")
PLACEHOLDER_RE = re.compile(r"\[[^\]]{2,}\]|lorem ipsum", re.IGNORECASE)


class Field:
    def __init__(self, value: Any, confidence: float, source: str = "rules") -> None:
        self.value = value
        self.confidence = confidence
        self.source = source


class Extraction:
    def __init__(
        self,
        document_type: str,
        fields: dict[str, Field] | None = None,
        line_items: list[dict[str, Any]] | None = None,
        extras: dict[str, Any] | None = None,
    ) -> None:
        self.document_type = document_type
        self.fields = fields if fields is not None else {}
        self.line_items = line_items if line_items is not None else []
        self.extras = extras if extras is not None else {}

    def set(self, key: str, value: Any, confidence: float, source: str = "rules") -> None:
        if value is None or value == "" or value == []:
            return
        self.fields[key] = Field(value, confidence, source)

    def get(self, key: str) -> Any:
        found = self.fields.get(key)
        return found.value if found else None


# ───────────────────────────── classification ─────────────────────────────


def classify(text: str) -> tuple[str, float]:
    low = text.lower()

    def n(pattern: str, cap: int) -> int:
        return min(len(re.findall(pattern, low)), cap)

    scores = {
        "invoice": (
            2 * n(r"\binvoice\b", 6)
            + 3 * n(r"invoice\s*(?:#|no\b|number|date)", 2)
            + 2 * n(r"bill(?:ed)?\s*to", 1)
            + 2 * n(r"tax invoice", 1)
            + n(r"due date", 1)
        ),
        "purchase-order": (
            4 * n(r"purchase order", 2)
            + 3 * n(r"\bp\.?\s?o\.?\s*(?:#|no\b|number|date)", 2)
            + 2 * n(r"\bvendor\b", 1)
            + 2 * n(r"ship\s*to", 1)
            + n(r"promise date|delivery date|required by", 1)
        ),
        "contract": (
            2 * n(r"\bagreement\b", 5)
            + 3 * n(r"\bwhereas\b", 1)
            + 3 * n(r"governed by", 1)
            + 2 * n(r"terminat", 2)
            + 2 * n(r"confidential", 1)
            + 2 * n(r"\bparties\b|\bparty\b", 1)
        ),
    }
    best = max(scores, key=scores.get)
    top, total = scores[best], sum(scores.values())
    if top < 5:
        return "general", 0.5
    return best, round(min(0.99, top / total), 2)


# ───────────────────────────── line helpers ─────────────────────────────


def last_amount(text: str) -> float | None:
    cleaned = re.sub(r"\(?\s*\d+(?:\.\d+)?\s*%\s*\)?", " ", text)
    tokens = AMOUNT_TOKEN_RE.findall(cleaned)
    return parse_amount(tokens[-1]) if tokens else None


def amount_after(lines: list[str], label: str, positive_only: bool = False) -> float | None:
    pattern = re.compile(rf"^\s*(?:{label})\b[\s:|]*(?P<rest>.*)$", re.IGNORECASE)
    for line in lines:
        m = pattern.match(line)
        if not m:
            continue
        value = last_amount(m.group("rest"))
        if value is not None and (value > 0 or not positive_only):
            return value
    return None


def date_after(lines: list[str], label: str) -> str | None:
    pattern = re.compile(rf"(?:{label})[\s:|]*(?P<rest>.*)$", re.IGNORECASE)
    for index, line in enumerate(lines):
        m = pattern.search(line)
        if not m:
            continue
        parsed = parse_date(m.group("rest")[:40])
        if parsed:
            return parsed
        if not m.group("rest").strip() and index + 1 < len(lines):
            parsed = parse_date(lines[index + 1][:40])
            if parsed:
                return parsed
    return None


def line_after_heading(lines: list[str], heading: str, skip_addresses: bool = False) -> str | None:
    pattern = re.compile(rf"^\s*(?:{heading})\s*:?\s*$", re.IGNORECASE)
    for index, line in enumerate(lines):
        if not pattern.match(line):
            continue
        for candidate in lines[index + 1 : index + 4]:
            if skip_addresses and re.match(r"^\d", candidate):
                continue
            return candidate
    return None


def block_after_heading(lines: list[str], heading: str, max_lines: int = 4) -> str | None:
    pattern = re.compile(rf"^\s*(?:{heading})\s*:?\s*(?P<rest>.*)$", re.IGNORECASE)
    for index, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            parts = [m.group("rest")] if m.group("rest").strip() else []
            for following in lines[index + 1 : index + 1 + max_lines]:
                if " | " in following:  # reached a table row
                    break
                parts.append(following)
            return ", ".join(p for p in parts if p).strip() or None
    return None


def reference_number(text: str, label: str) -> str | None:
    for m in re.finditer(rf"\b(?:{label})[\s:|#.-]*([A-Za-z0-9][A-Za-z0-9/_-]*)", text, re.IGNORECASE):
        if re.search(r"\d", m.group(1)):
            return m.group(1)
    return None


def payment_terms(text: str) -> tuple[str | None, int | None]:
    if m := re.search(r"\bpayment\s*terms[ \t:|.]*([^\n|]{2,120})", text, re.IGNORECASE):
        value = m.group(1).strip()
        if d := re.search(r"net\s*(\d+)", value, re.IGNORECASE):
            return f"Net {d.group(1)}", int(d.group(1))
        if d := re.search(r"within\s*(\d+)\s*days", value, re.IGNORECASE):
            return f"Net {d.group(1)}", int(d.group(1))
        return value[:60], None
    if m := re.search(r"\bnet\s*(\d{1,3})\b", text, re.IGNORECASE):
        return f"Net {m.group(1)}", int(m.group(1))
    if m := re.search(r"pay(?:able|ment)?[^.\n]{0,25}within\s*(\d{1,3})\s*days", text, re.IGNORECASE):
        return f"Net {m.group(1)}", int(m.group(1))
    return None, None


# ───────────────────────────── line items ─────────────────────────────

_ROLE_PATTERNS = {
    "qty": re.compile(r"\bqty\b|quantity|\bhrs\b|hours", re.IGNORECASE),
    "unit_price": re.compile(r"unit\s*price|\brate\b|\bprice\b|unit\s*cost", re.IGNORECASE),
    "amount": re.compile(r"amount|total|extended", re.IGNORECASE),
}
_TAX_HEADER = re.compile(r"tax|gst|vat", re.IGNORECASE)
_DESCRIPTION_HEADER = re.compile(r"description|item|service|product|particulars|details", re.IGNORECASE)
_SUMMARY_ROW = re.compile(r"^\s*(sub\s*total|total|tax|shipping|freight|discount|balance)\b", re.IGNORECASE)


def extract_line_items(markdown: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for table in extract_tables(markdown):
        header_index = next(
            (
                i
                for i, row in enumerate(table)
                if any(_DESCRIPTION_HEADER.search(c) for c in row)
                and any(_ROLE_PATTERNS["qty"].search(c) or _ROLE_PATTERNS["amount"].search(c) for c in row)
            ),
            None,
        )
        if header_index is None:
            continue

        header = table[header_index]
        roles: dict[str, int] = {}
        for role in ("qty", "unit_price", "amount"):
            matches = [
                i
                for i, cell in enumerate(header)
                if _ROLE_PATTERNS[role].search(cell)
                and i not in roles.values()
                and not (role == "amount" and _TAX_HEADER.search(cell))
            ]
            if matches:
                roles[role] = matches[-1] if role == "amount" else matches[0]

        body = [row + [""] * (len(header) - len(row)) for row in table[header_index + 1 :]]
        candidates = [i for i in range(len(header)) if i not in roles.values()]
        if not candidates:
            continue
        numeric_cols = [roles[r] for r in ("qty", "amount") if r in roles]
        data_rows = [r for r in body if any(parse_amount(r[c]) is not None for c in numeric_cols)] or body
        desc_col = max(candidates, key=lambda c: sum(len(r[c]) for r in data_rows) / max(len(data_rows), 1))

        for row in body:
            description = row[desc_col].strip()
            if not description or _SUMMARY_ROW.match(description):
                continue
            item = {
                "description": description,
                "quantity": parse_amount(row[roles["qty"]]) if "qty" in roles else None,
                "unit_price": parse_amount(row[roles["unit_price"]]) if "unit_price" in roles else None,
                "amount": parse_amount(row[roles["amount"]]) if "amount" in roles else None,
            }
            if item["quantity"] is None and item["amount"] is None:
                continue
            items.append(item)
    return items


# ───────────────────────────── invoice / purchase order ─────────────────────────────


def _apply_totals(ex: Extraction, lines: list[str]) -> None:
    total = None
    for label in (
        "grand\\s*total",
        "total\\s*due",
        "amount\\s*due",
        "due\\s*amount",
        "balance\\s*due",
        "total\\s*amount",
        "invoice\\s*total",
        "order\\s*total",
    ):
        total = amount_after(lines, label)
        if total is not None:
            break
    if total is None:
        total = amount_after(lines, "total", positive_only=True)
    ex.set("total_amount", total, 0.9)
    ex.set("subtotal", amount_after(lines, "sub\\s*total"), 0.9)
    ex.set("discount", amount_after(lines, "discount"), 0.85)
    ex.set("shipping", amount_after(lines, "freight|shipping(?:\\s*(?:&|and)\\s*handling)?"), 0.85)

    tax_pattern = re.compile(r"^\s*(?:sales\s*tax|tax|vat|igst)\b(?P<rest>.*)$", re.IGNORECASE)
    for line in lines:
        m = tax_pattern.match(line)
        if m and (amount := last_amount(m.group("rest"))) is not None:
            ex.set("tax_amount", amount, 0.85)
            ex.set("tax_rate", parse_percent(m.group("rest")), 0.85)
            break


def _apply_tax_ids(ex: Extraction, text: str) -> None:
    if m := GSTIN_RE.search(text):
        ex.set("gstin", m.group(0), 0.95)
        ex.set("pan", m.group(2), 0.9)
    if pan := PAN_RE.search(text):
        ex.set("pan", pan.group(0), 0.9)
    if m := re.search(r"\b(?:tax\s*id|vat\s*(?:no\.?|number|id)|ein|tin)[\s:|#]*([A-Z0-9-]{6,})", text, re.IGNORECASE):
        ex.set("tax_id", m.group(1), 0.8)


def extract_invoice(markdown: str) -> Extraction:
    text = to_plain_text(markdown)
    lines = non_empty_lines(text)
    ex = Extraction("invoice")

    ex.set("invoice_number", reference_number(text, r"invoice\s*(?:#|no\.?|number)"), 0.92)
    ex.set("invoice_date", date_after(lines, r"invoice\s*date|date\s*of\s*issue|issue\s*date"), 0.92)
    ex.set("due_date", date_after(lines, r"due\s*date|payment\s*due"), 0.9)
    ex.set("po_number", reference_number(text, r"(?:p\.?o\.?|purchase\s*order|order)\s*(?:#|no\.?|number)"), 0.85)

    vendor = line_after_heading(lines, r"from|vendor|supplier|seller", skip_addresses=True)
    if vendor:
        ex.set("vendor_name", vendor, 0.85)
    elif holder := line_after_heading(lines, r"account\s*holder(?:\s*name)?"):
        ex.set("vendor_name", holder, 0.65)

    customer = line_after_heading(lines, r"billed?\s*to|bill\s*to|customer|sold\s*to")
    if not customer:
        for line in lines:
            if m := re.match(r"^to\s*:\s*([A-Za-z][A-Za-z &.'-]*?)(?=\s+\d|\s*,|$)", line, re.IGNORECASE):
                customer = m.group(1).strip()
                break
    ex.set("customer_name", customer, 0.85)

    terms, days = payment_terms(text)
    ex.set("payment_terms", terms, 0.85)
    ex.set("payment_terms_days", days, 0.85)
    ex.set("currency", detect_currency(text), 0.8)

    _apply_tax_ids(ex, text)
    _apply_totals(ex, lines)
    ex.line_items = extract_line_items(markdown)
    return ex


def extract_purchase_order(markdown: str) -> Extraction:
    text = to_plain_text(markdown)
    lines = non_empty_lines(text)
    ex = Extraction("purchase-order")

    ex.set("po_number", reference_number(text, r"\bp\.?\s?o\.?\s*(?:#|no\.?|number)|purchase\s*order\s*(?:#|no\.?|number)"), 0.92)
    ex.set("order_date", date_after(lines, r"p\.?\s?o\.?\s*date|order\s*date|^date"), 0.9)
    ex.set("delivery_date", date_after(lines, r"promise\s*date|delivery\s*date|ship\s*date|required\s*by|expected\s*delivery"), 0.88)

    ex.set("vendor_name", line_after_heading(lines, r"vendor|supplier|seller", skip_addresses=True), 0.85)
    buyer = line_after_heading(lines, r"bill\s*to|buyer|purchaser")
    ex.set("buyer_name", buyer or (lines[0] if lines else None), 0.8 if buyer else 0.5)
    ex.set("ship_to", block_after_heading(lines, r"ship\s*to", 3), 0.8)

    terms, days = payment_terms(text)
    ex.set("payment_terms", terms, 0.85)
    ex.set("payment_terms_days", days, 0.85)
    ex.set("currency", detect_currency(text), 0.8)

    _apply_tax_ids(ex, text)
    _apply_totals(ex, lines)
    ex.line_items = extract_line_items(markdown)
    return ex


# ───────────────────────────── contract ─────────────────────────────

_PARTY_RE = re.compile(
    r"^(provider|client|customer|supplier|vendor|company|licensor|licensee|buyer|seller|contractor|consultant)\s*:?\s*\|?\s*([^,|(]{3,80})",
    re.IGNORECASE,
)
_CLAUSES = {
    "term_and_renewal": r"initial\s+(?:term|period)|automatically\s+renew|renewal",
    "payment": r"payment|invoice|fees",
    "confidentiality": r"confidential",
    "intellectual_property": r"intellectual property|work product|work made for hire",
    "liability": r"limitation of liability|aggregate liability|liability",
    "indemnification": r"indemnif",
    "termination": r"terminat",
    "governing_law": r"governed by|governing law",
    "data_protection": r"data protection|personal data|gdpr",
}


def extract_contract(markdown: str) -> Extraction:
    text = to_plain_text(markdown)
    lines = non_empty_lines(text)
    low = text.lower()
    ex = Extraction("contract")

    ex.set("title", lines[0] if lines else None, 0.8)
    if m := re.search(r"reference\s*(?:no\.?|number)[\s:|#]*([A-Z0-9][\w-]{3,})", text, re.IGNORECASE):
        ex.set("agreement_reference", m.group(1), 0.9)

    parties = []
    for line in lines:
        if m := _PARTY_RE.match(line):
            parties.append({"role": m.group(1).title(), "name": m.group(2).strip(" .:")})
    if not parties:
        if m := re.search(r"between\s+(.{3,80}?)\s+and\s+(.{3,80}?)[,(]", text, re.IGNORECASE | re.DOTALL):
            parties = [{"role": "Party", "name": m.group(1).strip()}, {"role": "Party", "name": m.group(2).strip()}]
    ex.set("parties", parties, 0.85 if parties else 0.0)

    if m := re.search(r"as of\s+([A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", text, re.IGNORECASE):
        ex.set("effective_date", parse_date(m.group(1)), 0.9)
    else:
        ex.set("effective_date", date_after(lines, r"effective\s*date"), 0.8)

    if m := re.search(r"(?:initial\s+(?:term|period)\s+of|for\s+a\s+term\s+of)\s+(?:[a-z-]+\s+)?\(?(\d+)\)?\s*(month|year)", low):
        months = int(m.group(1)) * (12 if m.group(2) == "year" else 1)
        ex.set("term_months", months, 0.85)

    auto = bool(re.search(r"automatically\s+renew|auto-?renew|renews\s+automatically|automatic\s+renewal", low))
    ex.fields["auto_renewal"] = Field(auto, 0.85)
    if m := re.search(r"non-renewal\s+at\s+least\s+(\d+)\s+days|(?:at least|not less than)\s+(\d+)\s+days\s+(?:prior|before)", low):
        ex.set("renewal_notice_days", int(m.group(1) or m.group(2)), 0.85)

    terms, days = payment_terms(text)
    ex.set("payment_terms", terms, 0.85)
    ex.set("payment_terms_days", days, 0.85)
    if m := re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:usd)?\s*per\s*(month|year|quarter)", text, re.IGNORECASE):
        ex.set("recurring_fee", f"${m.group(1)} per {m.group(2).lower()}", 0.85)
    if m := re.search(r"(\d+(?:\.\d+)?)\s*%\s*per\s*month", low):
        ex.set("late_interest_pct_month", float(m.group(1)), 0.85)

    if m := re.search(r"laws\s+of\s+(?:the\s+)?(?:state\s+of\s+|commonwealth\s+of\s+)?([A-Z][A-Za-z ]+?)(?:,|\.|\s+without|\s+and\b)", text):
        ex.set("governing_law", m.group(1).strip(), 0.88)
    if m := re.search(r"cure.{0,80}?within\s+(\d+)\s*(?:calendar\s*)?days", low, re.DOTALL):
        ex.set("cure_period_days", int(m.group(1)), 0.85)
    if m := re.search(r"confidential.{0,300}?\((\d+)\)\s+years?", low, re.DOTALL):
        ex.set("confidentiality_years", int(m.group(1)), 0.8)
    ex.set("liability_cap", bool(re.search(r"shall not exceed|liability cap|capped at", low)), 0.8)

    ex.extras["clauses"] = {name: bool(re.search(pattern, low)) for name, pattern in _CLAUSES.items()}
    return ex


# ───────────────────────────── entry point ─────────────────────────────


def extract(document_type: str, markdown: str) -> Extraction:
    if document_type == "invoice":
        return extract_invoice(markdown)
    if document_type == "purchase-order":
        return extract_purchase_order(markdown)
    if document_type == "contract":
        return extract_contract(markdown)
    lines = non_empty_lines(to_plain_text(markdown))
    ex = Extraction("general")
    ex.set("title", lines[0] if lines else None, 0.6)
    ex.set("summary", " ".join(lines)[:300], 0.5)
    return ex
