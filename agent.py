"""DocIntel agent for Fetch.ai Agentverse.

Works as a Hosted Agent (Agentverse runs it) or locally with mailbox=True.
Chat in:  a document URL, pasted text, or `sample <name>`, plus a type
          (`invoice`, `purchase-order`, `contract`, `general`, or `auto`).
Chat out: extracted fields, validation, exception queue.

    python agent.py          # local mailbox
    # hosted: paste this project into Agentverse Build and click Run
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    ResourceContent,
    TextContent,
    chat_protocol_spec,
)

try:
    from docintel.config import PROJECT_ROOT, get_settings
    from docintel.extract import parse_requested_type
    from docintel.format import format_report
    from docintel.mineru import MinerUError
    from docintel.pipeline import analyze_document, analyze_markdown
except ImportError:  # Agentverse Hosted flattens docintel/ next to agent.py
    from config import PROJECT_ROOT, get_settings
    from extract import parse_requested_type
    from format import format_report
    from mineru import MinerUError
    from pipeline import analyze_document, analyze_markdown

settings = get_settings()
logger = logging.getLogger("docintel-agent")

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)

HELP = """**DocIntel agent** reads business documents and checks them against business rules.

Send me a document, then tell me its type so I use the right extractor:
- `invoice` — totals, GSTIN, due date, line items
- `purchase-order` (or `po`) — PO number, delivery date, line math
- `contract` — parties, term, auto-renewal, clauses
- `general` — title and summary only
- `auto` — I'll classify it myself

How to send a document:
- a link to a PDF / image / Office file (`https://.../invoice.pdf`)
- `sample <name>` to try a built-in document: {samples}
- the document text pasted straight into the chat

You can put the type in the same message (`invoice sample invoice-sliced`) or send it after I ask. Add the word `json` to also get the raw JSON."""

ASK_TYPE = """I have **{filename}**. Which extractor should I use?

Reply with one of:
- `invoice`
- `purchase-order` (or `po`)
- `contract`
- `general`
- `auto` (I'll classify it)

You can also send the type with the document next time, e.g. `invoice sample invoice-sliced`."""


def sample_names() -> list[str]:
    return sorted(p.stem for p in settings.samples_dir.glob("*.pdf")) if settings.samples_dir.exists() else []


def download(url: str) -> tuple[bytes, str]:
    limit = settings.max_download_mb * 1024 * 1024
    with requests.get(url, timeout=45, stream=True) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"File is larger than {settings.max_download_mb} MB.")
            chunks.append(chunk)
    name = unquote(Path(urlparse(url).path).name) or "document.pdf"
    return b"".join(chunks), name


def _load_payload(ref: dict) -> dict:
    kind = ref["kind"]
    if kind == "url":
        data, filename = download(ref["url"])
        return {"data": data, "filename": filename, "markdown": None}
    if kind == "sample":
        path = settings.samples_dir / f"{ref['sample']}.pdf"
        return {"data": path.read_bytes(), "filename": path.name, "markdown": None}
    return {"data": None, "filename": ref.get("filename", "pasted-text"), "markdown": ref["markdown"]}


def _run_pipeline(ref: dict, document_type: str | None, want_json: bool, registry: dict) -> str:
    override = None if document_type in (None, "auto") else document_type
    payload = _load_payload(ref)
    if payload.get("markdown") is not None:
        result = analyze_markdown(payload["markdown"], payload["filename"], override, settings, registry)
    else:
        result = analyze_document(payload["data"], payload["filename"], override, settings, registry)
    reply = format_report(result)
    if want_json:
        reply += "\n\n```json\n" + json.dumps(result, indent=2, ensure_ascii=False) + "\n```"
    return reply


def handle_request(
    text: str,
    resource_uris: list[str] | None = None,
    pending: dict | None = None,
    registry: dict | None = None,
) -> tuple[str, bool, dict | None, dict]:
    """Blocking: (reply, end_session, next_pending, registry). Safe in a worker thread.

    Pending is a JSON-safe reference (url / sample name / markdown), never file bytes,
    so it can live in Agentverse ctx.storage between hosted invocations.
    """
    text = (text or "").strip()
    want_json = bool(re.search(r"\bjson\b", text, re.IGNORECASE))
    lowered = text.lower()
    names = sample_names()
    registry = dict(registry or {})
    pending = dict(pending) if pending else None

    try:
        urls = list(resource_uris or []) + URL_RE.findall(text)
        sample = next((n for n in names if n.lower() in lowered), None)
        pasted = (not sample) and (not urls) and len(text) > 200 and "\n" in text
        type_src = text.split("\n", 1)[0] if pasted else text
        requested = parse_requested_type(type_src, names)
        ref: dict | None = None

        if sample:
            ref = {"kind": "sample", "sample": sample, "filename": f"{sample}.pdf", "want_json": want_json}
        elif urls:
            if not urls[0].lower().startswith(("http://", "https://")):
                return (
                    f"I can only fetch http(s) links, not `{urls[0].split(':')[0]}://` resources. Send a public link or paste the text.",
                    True,
                    None,
                    registry,
                )
            name = unquote(Path(urlparse(urls[0]).path).name) or "document.pdf"
            ref = {"kind": "url", "url": urls[0], "filename": name, "want_json": want_json}
        elif lowered.startswith("sample") or "samples" in lowered:
            return (
                "Available samples: " + ", ".join(f"`{n}`" for n in names) + ".\nSend e.g. `invoice sample invoice-sliced`.",
                True,
                None,
                registry,
            )
        elif pasted:
            first, _, rest = text.partition("\n")
            markdown = rest.strip() if requested and rest.strip() else text
            ref = {"kind": "markdown", "markdown": markdown, "filename": "pasted-text", "want_json": want_json}

        if ref is not None:
            if requested:
                return _run_pipeline(ref, requested, want_json, registry), True, None, registry
            return ASK_TYPE.format(filename=ref["filename"]), False, ref, registry

        if requested and pending:
            return _run_pipeline(pending, requested, want_json or pending.get("want_json", False), registry), True, None, registry
        if requested and not pending:
            return "Send a document first (a link, pasted text, or `sample <name>`), then I'll use that type's extractor.", True, None, registry

        if pending:
            return ASK_TYPE.format(filename=pending["filename"]), False, pending, registry
        return HELP.format(samples=", ".join(f"`{n}`" for n in names) or "none installed"), True, None, registry

    except MinerUError as exc:
        return f"I could not parse that document: {exc}", True, None, registry
    except requests.RequestException as exc:
        return f"I could not download that link: {exc}", True, None, registry
    except Exception as exc:  # never leave the user without a reply
        logger.exception("processing failed")
        return f"Processing failed: {exc}", True, None, registry


# On Agentverse Hosted, Agent() wraps the pre-injected instance; name/seed/mailbox are ignored.
readme = PROJECT_ROOT / "README.md"
agent = Agent(
    name=settings.agent_name,
    seed=settings.agent_seed,
    port=settings.agent_port,
    mailbox=True,
    publish_agent_details=True,
    readme_path=str(readme) if readme.exists() else None,
)

chat = Protocol(spec=chat_protocol_spec)


def _pending_key(sender: str) -> str:
    return f"pending:{sender}"


def _store_get(ctx: Context, key: str, default):
    raw = ctx.storage.get(key)
    if raw in (None, "", {}, []):
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


def _store_set(ctx: Context, key: str, value) -> None:
    ctx.storage.set(key, json.dumps(value) if value not in (None, "") else "")


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))

    uris = [
        (c.resource[0] if isinstance(c.resource, list) else c.resource).uri
        for c in msg.content
        if isinstance(c, ResourceContent)
    ]
    ctx.logger.info("request from %s: %r %s" % (sender[:16], msg.text()[:80], uris))
    pending = _store_get(ctx, _pending_key(sender), None)
    registry = _store_get(ctx, "registry", {}) or {}
    reply, done, next_pending, registry = await asyncio.to_thread(
        handle_request, msg.text(), uris, pending, registry
    )
    _store_set(ctx, _pending_key(sender), next_pending)
    _store_set(ctx, "registry", registry)
    content: list = [TextContent(text=reply)]
    if done:
        content.append(EndSessionContent())
    await ctx.send(sender, ChatMessage(content=content))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug("ack from %s for %s" % (sender[:16], msg.acknowledged_msg_id))


agent.include(chat, publish_manifest=True)


@agent.on_event("startup")
async def on_startup(ctx: Context) -> None:
    try:
        from docintel.mineru import MinerUClient
    except ImportError:
        from mineru import MinerUClient

    ctx.logger.info("DocIntel agent address: %s" % ctx.agent.address)
    mineru = MinerUClient(settings)
    mineru_mode = "v4 token" if settings.mineru_api_token else "public agent API (no token)"
    try:
        mineru_status = "reachable" if mineru.health() else "NOT reachable"
    except Exception as exc:
        mineru_status = "health check failed: %s" % exc
    ctx.logger.info("MinerU API %s (%s): %s" % (settings.mineru_api_url, mineru_mode, mineru_status))
    ctx.logger.info("Gemini gap-filling: %s" % ("enabled" if settings.gemini_api_keys else "off (rules only)"))
    hosted = bool(os.getenv("AGENTVERSE_URL") or os.getenv("AV_HOSTING"))
    ctx.logger.info("Mode: %s" % ("Agentverse hosted" if hosted else "Agentverse mailbox / local"))


if __name__ == "__main__":
    agent.run()
