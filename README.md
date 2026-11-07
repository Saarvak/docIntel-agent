# DocIntel

![tags:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3) ![tags:document-ai](https://img.shields.io/badge/document--ai-3D8BD3)

Reads business documents and checks them against business rules. You send a document; it replies with extracted fields, line items, pass/warn/fail checks, and the exception queue a reviewer should look at.

## Website

- [DocIntel](https://www.docintelai.saarvak.in/)
- [Saarvak](https://saarvak.in/)
- [LinkedIn](https://www.linkedin.com/company/saarvak/)

## How to use

Send a public `http(s)` link to a PDF, image, or Office file, **and** the document type. If you omit the type, the agent asks before extracting.

- `invoice` — totals, vendor, due date, line items, GSTIN
- `purchase-order` (or `po`) — PO number, delivery date, line math
- `contract` — parties, term, auto-renewal, clauses
- `general` — title and summary only
- `auto` — classify it automatically

Examples:

```
invoice https://example.com/invoice.pdf
po https://example.com/order.pdf
contract https://example.com/msa.pdf
```

You can also paste the document text into the chat. Add the word `json` if you want the raw JSON as well.

## What it checks

| Document | Rules |
|---|---|
| Invoice | required fields, due date vs invoice date, qty × unit price, line items vs subtotal / total, GSTIN, missing PO reference, duplicates |
| Purchase order | required fields, delivery date vs order date, line math, totals, unfilled-template placeholders, duplicates |
| Contract | parties, effective date, governing law, termination / confidentiality / liability / payment clauses, auto-renewal |

A failed rule becomes an **exception** (`ap_exceptions`, `procurement_exceptions`, or `legal_review`). Warnings do not block. Documents with no failures and no warnings are marked straight-through.

## Limits

- Public `http(s)` links only. File attachments are not supported yet.
- Layouts that do not match common invoice, PO, or MSA patterns should be sent to review rather than trusted.
