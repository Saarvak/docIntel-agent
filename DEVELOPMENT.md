# docIntel-agent — local setup and deploy

Developer instructions. The public Agentverse page uses `README.md` (overview only).

## Setup (Windows PowerShell)

```powershell
cd docIntel-agent
python -m venv .venv
.\.venv\Scripts\activate
pip install --timeout 300 --retries 10 -r requirements.txt
copy .env.example .env      # then edit AGENT_SEED and MINERU_API_TOKEN
```

### MinerU API (needed for your own PDFs)

Do **not** install or run MinerU on this machine. The agent calls the hosted API at `https://mineru.net`.

1. Create a token at [mineru.net/apiManage/token](https://mineru.net/apiManage/token).
2. Put it in `.env` as `MINERU_API_TOKEN=...`.

With a token, files go through the precise v4 API (up to 200 MB / 200 pages). Without a token, the public agent API is used instead (about 10 MB / 20 pages, IP rate-limited).

Every PDF, including `sample <name>`, is sent to the MinerU API. There is no local Markdown cache.

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
python test_client.py "invoice sample invoice-foobar-labs"
python test_client.py "sample invoice-sliced"   # agent asks for the type, then send invoice / po / contract
```

Samples: `invoice-sliced` (clean, passes), `invoice-foobar-labs` (totals do not reconcile), `po-procurify` (no PO number), `po-tobii` (unfilled template), `contract-msa-nexus` (auto-renewal).

## Profile a GCP VM

This measures the **agent** on the VM while it waits on the MinerU cloud API. MinerU itself is not installed on the VM.

```bash
cd docIntel-agent
source .venv/bin/activate
python profile_run.py --require-mineru
# or, same thing plus extra host dumps:
bash scripts/profile_gcp.sh
bash scripts/profile_gcp.sh --repeat 2 /path/to/another.pdf
```

When it exits, open the newest folder under `profiles/`:

| File | What to look at |
|---|---|
| `REPORT.md` | Instance type, RAM/CPU peaks, per-document stage times (parse = API wait) |
| `summary.json` | Same numbers as JSON |
| `timeseries.csv` | Sampled RAM / load every 0.5s |
| `documents.json` | One row per file (warmup pass is labelled) |
| `host.json` | GCP metadata, CPU count |

`--require-mineru` fails if `https://mineru.net` is unreachable. Every file is parsed through the API.

## Host on Agentverse (free, no VM)

Agentverse runs the agent for you. MinerU stays on mineru.net. Do **not** run `avctl hosting init` in this folder — it overwrites `agent.py`.

### Deploy with avctl (preferred)

```powershell
# once per machine
Invoke-WebRequest -UseBasicParsing https://github.com/fetchai/avctl/releases/latest/download/install.ps1 | Invoke-Expression
# open a new terminal so PATH picks up avctl
cd docIntel-agent
avctl auth login

# Agentverse only accepts .py (folders are flattened). Do not upload .env.
$files = @(
  'agent.py',
  'docintel/config.py','docintel/extract.py','docintel/format.py',
  'docintel/llm.py','docintel/mineru.py','docintel/pipeline.py',
  'docintel/text.py','docintel/validate.py'
) -join ','
avctl hosting deploy -n DocIntel --no-dependency-check -f $files
```

Live agent: [`agent1q0rnzvuslxf2hk9gul4alja6x3v8vz238aap4syehpjcl76cry8xqrn9kcc`](https://agentverse.ai/agents/details/agent1q0rnzvuslxf2hk9gul4alja6x3v8vz238aap4syehpjcl76cry8xqrn9kcc)

MinerU secrets are already on the hosted agent. To change them, open **Build → Agent Secrets** (not the repo `.env`):

| Name | Value |
|---|---|
| `MINERU_API_TOKEN` | token from [mineru.net/apiManage/token](https://mineru.net/apiManage/token) |
| `MINERU_API_URL` | `https://mineru.net` |
| `MINERU_BACKEND` | `pipeline` |
| `MINERU_LANG_LIST` | `en` |

Chat a public PDF link plus a type, e.g. `invoice https://example.com/invoice.pdf`. Sample PDFs cannot be uploaded to Hosted (only `.py`). Log in to Agentverse at least once every **30 days**, or the hosted agent is deactivated.

Pending document type and duplicate detection live in **Agent Storage** (hosted agents reset Python globals after each message).

### Paste into Build instead

1. Open [agentverse.ai](https://agentverse.ai) → **Agents** → **Launch an Agent** → **Hosted**.
2. In **Build**, add these files at the **top level** (Agentverse flattens folders; do not put them in `docintel/`):

```
agent.py
config.py
extract.py
format.py
llm.py
mineru.py
pipeline.py
text.py
validate.py
```

3. Add the same **Agent Secrets** as above. Click **Run**. Send a public PDF link plus a type — sample PDFs cannot be hosted.

### Local mailbox instead

The address comes from `AGENT_SEED`. Run `python agent.py`, open the **Agent inspector** link, **Connect → Mailbox**. Keep that process running. A hosted Agentverse agent cannot be taken over by this local process; they are different addresses.

## Optional: Gemini gap-filling

Set `GEMINI_API_KEY` (or `GEMINI_API_KEY1`, `GEMINI_API_KEY2`, ...) to let Gemini fill fields the rules missed. Rule-extracted values are never overwritten, and LLM-filled fields are marked `"source": "llm"` in the JSON.
