"""Helpers for MinerU Markdown: HTML tables, plain-text lines, and value normalization."""

from __future__ import annotations

import html
import re
from datetime import date
from html.parser import HTMLParser

TABLE_RE = re.compile(r"<table>.*?</table>", re.IGNORECASE | re.DOTALL)

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"

CURRENCY_SYMBOLS = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
CURRENCY_CODES = {"INR", "USD", "EUR", "GBP", "AUD", "CAD", "SGD", "AED", "JPY", "CHF"}


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._colspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell = []
            span = dict(attrs).get("colspan") or "1"
            self._colspan = int(span) if span.isdigit() else 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            text = " ".join("".join(self._cell).split())
            self._row.append(text)
            # Pad spanned columns so headers and data cells stay aligned.
            self._row.extend([""] * (self._colspan - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def extract_tables(markdown: str) -> list[list[list[str]]]:
    tables = []
    for match in TABLE_RE.finditer(markdown):
        parser = _TableParser()
        parser.feed(match.group())
        rows = [r for r in parser.rows if any(c.strip() for c in r)]
        if rows:
            tables.append(rows)
    return tables


def to_plain_text(markdown: str) -> str:
    """Flatten tables into `cell | cell` lines and drop Markdown noise so regexes see one fact per line."""

    def table_to_lines(match: re.Match[str]) -> str:
        parser = _TableParser()
        parser.feed(match.group())
        lines = [" | ".join(c for c in row if c) for row in parser.rows]
        return "\n" + "\n".join(line for line in lines if line) + "\n"

    text = TABLE_RE.sub(table_to_lines, markdown)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = text.replace("\\$", "$").replace("\\_", "_")
    return html.unescape(text)


def non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_amount(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace("\u00a0", " ")
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def parse_percent(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", value)
    return float(match.group(1)) if match else None


def parse_date(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()

    if m := re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text):
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := re.search(rf"\b{MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", text, re.IGNORECASE):
        return _iso(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
    if m := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{MONTH_RE},?\s+(\d{{4}})\b", text, re.IGNORECASE):
        return _iso(int(m.group(3)), MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
    if m := re.search(r"\b(\d{1,2})([/.-])(\d{1,2})\2(\d{2,4})\b", text):
        a, sep, b, year = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
        year = year + 2000 if year < 100 else year
        # Day-first when unambiguous or dot-separated (European); otherwise US month-first.
        if a > 12 or sep == ".":
            return _iso(year, b, a)
        return _iso(year, a, b)
    return None


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def detect_currency(text: str) -> str | None:
    for code in re.findall(r"\b[A-Z]{3}\b", text):
        if code in CURRENCY_CODES:
            return code
    counts = {code: text.count(symbol) for symbol, code in CURRENCY_SYMBOLS.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] else None


def approx_equal(a: float, b: float, tolerance_abs: float = 0.51, tolerance_pct: float = 0.005) -> bool:
    return abs(a - b) <= max(tolerance_abs, abs(b) * tolerance_pct)
