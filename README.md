# docIntel-agent

![tags:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3) ![tags:document-ai](https://img.shields.io/badge/document--ai-3D8BD3)

Fetch.ai uAgent that reads business documents and checks them against business rules.

**Send it:** a link to a PDF / image / Office file, a pasted document text, or `sample <name>`.
**It replies with:** document type, extracted fields and line items, validation results, and the exception queue a human reviewer should look at.

```
chat message -> MinerU (PDF -> Markdown/JSON) -> classify -> extract fields + tables -> validate -> reply
```

## What it validates

| Document | Rules |
|---|---|
| Invoice | required fields, due date vs invoice date, qty x unit price, line items vs subtotal / total, subtotal + tax - discount + shipping = total, GSTIN state code and embedded PAN, missing PO reference, duplicate detection |
| Purchase order | required fields, delivery date vs order date, line math, subtotal / tax-rate / total reconciliation, unfilled-template placeholders, duplicates |
| Contract | parties, effective date, governing law, termination / confidentiality / liability / payment clauses, auto-renewal owner, late-interest policy limit |

A failed rule becomes an **exception** routed to a queue (`ap_exceptions`, `procurement_exceptions`, `legal_review`). Warnings do not block. Documents with no failures and no warnings are marked straight-through.

## Setup (Windows PowerShell)

```powershell
cd docIntel-agent
python -m venv .venv
.\.venv\Scripts\activate
pip install --timeout 300 --retries 10 -r requirements.txt
copy .env.example .env      # then edit AGENT_SEED etc.
```

### MinerU (needed for your own PDFs)

Install it in a **separate** environment (it pulls in PyTorch) and keep it running:

```powershell
python -m venv ..\mineru-env
..\mineru-env\Scripts\pip install --timeout 300 -U "mineru[core]"
..\mineru-env\Scripts\mineru-models-download      # choose: pipeline
..\mineru-env\Scripts\mineru-api --host 0.0.0.0 --port 8000
```

Without MinerU, the five PDFs in `samples/` still work: their saved MinerU Markdown is reused when the file bytes are identical.

## Test locally

```powershell
# 1. Unit tests on the sample documents (no MinerU, no network)
python -m unittest discover -s tests -v

# 2. The pipeline alone
python analyze.py samples\invoice-foobar-labs.pdf
python analyze.py path\to\your.pdf --json

# 3. The agent: terminal 1
python agent.py
#    terminal 2
python test_client.py "sample invoice-foobar-labs"
python test_client.py "https://example.com/some-invoice.pdf json"
```

Samples: `invoice-sliced` (clean, passes), `invoice-foobar-labs` (totals do not reconcile), `po-procurify` (no PO number), `po-tobii` (unfilled template), `contract-msa-nexus` (auto-renewal).

## Connect to Agentverse

The agent address is derived from `AGENT_SEED`.

- **Existing agent created from local code:** put its seed in `AGENT_SEED` to keep its address.
- **Agent created inside Agentverse (hosted):** local code cannot take over that address. Run this agent with a new seed and `AGENT_MAILBOX=true`.

For a mailbox: set `AGENT_MAILBOX=true`, run `python agent.py`, open the **Agent inspector** link printed in the log, click **Connect -> Mailbox**, then find the agent in Agentverse under *Local agents*. Keep `python agent.py` and MinerU running while it is in use.

## Optional: Gemini gap-filling

Set `GEMINI_API_KEY` (or `GEMINI_API_KEY1`, `GEMINI_API_KEY2`, ...) to let Gemini fill fields the rules missed. Rule-extracted values are never overwritten, and LLM-filled fields are marked `"source": "llm"` in the JSON.

## Limits

- Fetches `http(s)` links only. Agentverse `agent-storage://` file attachments are not supported yet.
- Rules are tuned for common invoice, PO and MSA layouts; unusual layouts should be sent to review rather than trusted. Add more samples to `tests/` as you meet them.
- Duplicate detection is in memory and resets when the agent restarts.
