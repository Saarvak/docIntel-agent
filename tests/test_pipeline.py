"""Pipeline tests on the bundled sample documents (MinerU not required: saved Markdown is used).

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from docintel import analyze_document
from docintel.config import get_settings
from docintel.pipeline import analyze_markdown
from docintel.text import parse_amount, parse_date

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
# Never call an LLM in tests, and never reach a real MinerU.
SETTINGS = replace(get_settings(), gemini_api_keys=(), mineru_api_url="http://127.0.0.1:9", samples_dir=SAMPLES)


def run(name: str, registry=None) -> dict:
    return analyze_document((SAMPLES / f"{name}.pdf").read_bytes(), f"{name}.pdf", settings=SETTINGS, registry=registry)


def value(result: dict, key: str):
    return result["fields"][key]["value"]


def failed_rules(result: dict) -> set[str]:
    return {c["rule"] for c in result["validation"]["checks"] if c["status"] == "fail"}


class Normalization(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(parse_date("FEB 19, 2020"), "2020-02-19")
        self.assertEqual(parse_date("7/13/2020"), "2020-07-13")
        self.assertEqual(parse_date("25.12.2024"), "2024-12-25")
        self.assertEqual(parse_date("5th March 2021"), "2021-03-05")
        self.assertIsNone(parse_date("31/31/2020"))

    def test_amounts(self):
        self.assertEqual(parse_amount("₹36,000"), 36000)
        self.assertEqual(parse_amount("$2,325.00"), 2325.0)
        self.assertIsNone(parse_amount("n/a"))


class Invoices(unittest.TestCase):
    def test_clean_invoice_passes_straight_through(self):
        r = run("invoice-sliced")
        self.assertEqual(r["document_type"], "invoice")
        self.assertEqual(value(r, "invoice_number"), "INV-3337")
        self.assertEqual(value(r, "invoice_date"), "2016-01-25")
        self.assertEqual(value(r, "total_amount"), 93.5)
        self.assertEqual(value(r, "currency"), "USD")
        self.assertEqual(len(r["line_items"]), 1)
        self.assertEqual(r["validation"]["status"], "passed")
        self.assertFalse(r["needs_human_review"])

    def test_inconsistent_invoice_is_routed_to_ap_exceptions(self):
        r = run("invoice-foobar-labs")
        self.assertEqual(value(r, "invoice_number"), "003")
        self.assertEqual(value(r, "gstin"), "29VGCED1234K2Z6")
        self.assertEqual(len(r["line_items"]), 4)
        self.assertIn("totals.lines_vs_total", failed_rules(r))
        self.assertTrue(r["needs_human_review"])
        self.assertEqual({e["queue"] for e in r["validation"]["exceptions"]}, {"ap_exceptions"})
        gst = {c["rule"]: c["status"] for c in r["validation"]["checks"]}
        self.assertEqual(gst["tax_id.gstin_state"], "pass")
        self.assertEqual(gst["tax_id.gstin_pan"], "pass")

    def test_duplicate_invoice_is_rejected(self):
        registry: dict[str, str] = {}
        first = run("invoice-sliced", registry)
        again = analyze_markdown((SAMPLES / "invoice-sliced.md").read_text(encoding="utf-8"), "copy.pdf", settings=SETTINGS, registry=registry, document_id="doc_second")
        self.assertNotIn("duplicate.document", failed_rules(first))
        self.assertIn("duplicate.document", failed_rules(again))


class PurchaseOrders(unittest.TestCase):
    def test_missing_po_number_fails(self):
        r = run("po-procurify")
        self.assertEqual(r["document_type"], "purchase-order")
        self.assertIn("required.po_number", failed_rules(r))
        self.assertEqual(len(r["line_items"]), 4)
        self.assertAlmostEqual(value(r, "subtotal"), 1854.99)
        statuses = {c["rule"]: c["status"] for c in r["validation"]["checks"]}
        self.assertEqual(statuses["totals.lines_vs_subtotal"], "pass")
        self.assertEqual(statuses["totals.tax_rate"], "pass")
        self.assertEqual(statuses["line_items.qty_x_price"], "pass")

    def test_unfilled_template_is_flagged(self):
        r = run("po-tobii")
        self.assertEqual(value(r, "po_number"), "123456")
        self.assertEqual(value(r, "order_date"), "2020-07-13")
        self.assertEqual([i["description"] for i in r["line_items"]], ["Product XYZ", "Product ABC"])
        self.assertEqual(r["validation"]["status"], "passed_with_warnings")
        warned = {c["rule"] for c in r["validation"]["checks"] if c["status"] == "warn"}
        self.assertIn("quality.placeholders", warned)


class Contracts(unittest.TestCase):
    def test_msa_terms_are_extracted(self):
        r = run("contract-msa-nexus")
        self.assertEqual(r["document_type"], "contract")
        self.assertEqual(value(r, "effective_date"), "2026-03-01")
        self.assertEqual(value(r, "term_months"), 12)
        self.assertTrue(value(r, "auto_renewal"))
        self.assertEqual(value(r, "renewal_notice_days"), 60)
        self.assertEqual(value(r, "payment_terms_days"), 30)
        self.assertIn("Delaware", value(r, "governing_law"))
        self.assertEqual([p["name"] for p in value(r, "parties")][0], "Nexus Cloud Solutions LLC")
        self.assertEqual(r["validation"]["status"], "passed_with_warnings")
        warned = {c["rule"] for c in r["validation"]["checks"] if c["status"] == "warn"}
        self.assertIn("renewal.owner", warned)


class Classification(unittest.TestCase):
    def test_document_type_override(self):
        r = analyze_markdown("Just some meeting notes.\n\nNothing structured here.", settings=SETTINGS)
        self.assertEqual(r["document_type"], "general")
        with self.assertRaises(ValueError):
            analyze_markdown("x", document_type="receipt", settings=SETTINGS)


if __name__ == "__main__":
    unittest.main()
