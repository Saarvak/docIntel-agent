"""Optional Gemini gap-filler. Rules run first; the LLM only fills fields the rules could not find."""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from .config import Settings
    from .extract import Extraction
except ImportError:
    from config import Settings
    from extract import Extraction

logger = logging.getLogger(__name__)

SCHEMAS: dict[str, str] = {
    "invoice": '{"vendor_name": str, "customer_name": str, "invoice_number": str, "invoice_date": "YYYY-MM-DD", "due_date": "YYYY-MM-DD", "po_number": str, "currency": "ISO code", "subtotal": number, "tax_amount": number, "total_amount": number, "payment_terms": str}',
    "purchase-order": '{"po_number": str, "vendor_name": str, "buyer_name": str, "order_date": "YYYY-MM-DD", "delivery_date": "YYYY-MM-DD", "currency": "ISO code", "subtotal": number, "tax_amount": number, "total_amount": number, "payment_terms": str}',
    "contract": '{"effective_date": "YYYY-MM-DD", "governing_law": str, "payment_terms": str}',
}
LINE_ITEM_SCHEMA = '"line_items": [{"description": str, "quantity": number, "unit_price": number, "amount": number}]'


class GeminiFiller:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return bool(self.settings.gemini_api_keys)

    def fill(self, ex: Extraction, markdown: str) -> list[str]:
        """Fill missing fields in place. Returns the keys that were filled."""
        schema = SCHEMAS.get(ex.document_type)
        if not (self.available and schema):
            return []
        wanted_line_items = ex.document_type in {"invoice", "purchase-order"} and not ex.line_items
        prompt = (
            f"Extract data from this {ex.document_type} (Markdown from OCR). Return ONLY JSON with this shape, "
            f"using null for anything not present; do not guess.\n{schema[:-1]}"
            + (f", {LINE_ITEM_SCHEMA}" if wanted_line_items else "")
            + "}\nAmounts are plain numbers without currency symbols.\n\n---\n"
            + markdown[:12000]
        )
        data = self._call(prompt)
        if not data:
            return []

        filled = []
        for key, value in data.items():
            if key == "line_items":
                if wanted_line_items and isinstance(value, list):
                    ex.line_items = [
                        {k: i.get(k) for k in ("description", "quantity", "unit_price", "amount")}
                        for i in value
                        if isinstance(i, dict) and i.get("description")
                    ]
                    filled.append("line_items")
            elif key not in ex.fields and value not in (None, ""):
                ex.set(key, value, 0.8, "llm")
                filled.append(key)
        return filled

    def _call(self, prompt: str) -> dict[str, Any] | None:
        from google import genai
        from google.genai import types

        for api_key in self.settings.gemini_api_keys:
            client = genai.Client(api_key=api_key)
            for model in dict.fromkeys([self.settings.gemini_model, "gemini-flash-lite-latest"]):
                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
                    )
                    return json.loads((response.text or "").strip())
                except Exception as exc:  # quota, auth, bad JSON: try the next model or key
                    logger.warning("Gemini call failed on %s: %s", model, str(exc)[:150])
        return None
