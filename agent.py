"""DocIntel agent for Fetch.ai Agentverse.

Chat in:  a document URL (PDF, image, Office), a pasted document text, or `sample <name>`.
Chat out: classification, extracted fields, line items, business-rule validation and
          the exception queue, as Markdown.

    python agent.py
"""

from __future__ import annotations

import asyncio
import json
import logging
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

from docintel.config import PROJECT_ROOT, get_settings
from docintel.format import format_report
from docintel.mineru import MinerUError
from docintel.pipeline import analyze_document, analyze_markdown

settings = get_settings()
logger = logging.getLogger("docintel-agent")

# vendor|number|amount -> document id, for duplicate detection across the agent's lifetime
REGISTRY: dict[str, str] = {}

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)

HELP = """**DocIntel agent** reads business documents and checks them against business rules.

Send me one of:
- a link to a PDF / image / Office file (`https://.../invoice.pdf`)
- `sample <name>` to try a built-in document: {samples}
- the document text pasted straight into the chat

I return the document type, extracted fields and line items, validation results, and the exception queue a human reviewer should look at. Add the word `json` to also get the raw JSON."""


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


def handle_request(text: str, resource_uris: list[str] | None = None) -> str:
    """Blocking: turn one chat request into a reply. Safe to run in a worker thread."""
    text = (text or "").strip()
    want_json = bool(re.search(r"\bjson\b", text, re.IGNORECASE))
    lowered = text.lower()

    try:
        data: bytes | None = None
        filename = "document"
        markdown: str | None = None

        urls = list(resource_uris or []) + URL_RE.findall(text)
        sample = next((n for n in sample_names() if n.lower() in lowered), None)

        if sample:
            path = settings.samples_dir / f"{sample}.pdf"
            data, filename = path.read_bytes(), path.name
        elif urls:
            if not urls[0].lower().startswith(("http://", "https://")):
                return f"I can only fetch http(s) links, not `{urls[0].split(':')[0]}://` resources. Send a public link or paste the text."
            data, filename = download(urls[0])
        elif lowered.startswith("sample") or "samples" in lowered:
            return "Available samples: " + ", ".join(f"`{n}`" for n in sample_names()) + ".\nSend e.g. `sample invoice-sliced`."
        elif len(text) > 200 and "\n" in text:
            markdown = text
        else:
            return HELP.format(samples=", ".join(f"`{n}`" for n in sample_names()) or "none installed")

        if markdown is not None:
            result = analyze_markdown(markdown, "pasted-text", settings=settings, registry=REGISTRY)
        else:
            result = analyze_document(data, filename, settings=settings, registry=REGISTRY)  # type: ignore[arg-type]

        reply = format_report(result)
        if want_json:
            reply += "\n\n```json\n" + json.dumps(result, indent=2, ensure_ascii=False) + "\n```"
        return reply

    except MinerUError as exc:
        return f"I could not parse that document: {exc}"
    except requests.RequestException as exc:
        return f"I could not download that link: {exc}"
    except Exception as exc:  # never leave the user without a reply
        logger.exception("processing failed")
        return f"Processing failed: {exc}"


agent = Agent(
    name=settings.agent_name,
    seed=settings.agent_seed,
    port=settings.agent_port,
    mailbox=settings.agent_mailbox,
    publish_agent_details=True,
    readme_path=str(PROJECT_ROOT / "README.md") if (PROJECT_ROOT / "README.md").exists() else None,
)

chat = Protocol(spec=chat_protocol_spec)


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))

    uris = [
        (c.resource[0] if isinstance(c.resource, list) else c.resource).uri
        for c in msg.content
        if isinstance(c, ResourceContent)
    ]
    ctx.logger.info("request from %s: %r %s", sender[:16], msg.text()[:80], uris)
    reply = await asyncio.to_thread(handle_request, msg.text(), uris)
    await ctx.send(sender, ChatMessage(content=[TextContent(text=reply), EndSessionContent()]))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug("ack from %s for %s", sender[:16], msg.acknowledged_msg_id)


agent.include(chat, publish_manifest=True)


@agent.on_event("startup")
async def on_startup(ctx: Context) -> None:
    from docintel.mineru import MinerUClient

    ctx.logger.info("DocIntel agent address: %s", ctx.agent.address)
    ctx.logger.info("MinerU at %s: %s", settings.mineru_api_url, "reachable" if MinerUClient(settings).health() else "NOT reachable (using offline sample cache for known samples)")
    ctx.logger.info("Gemini gap-filling: %s", "enabled" if settings.gemini_api_keys else "off (rules only)")
    ctx.logger.info("Mode: %s", "Agentverse mailbox" if settings.agent_mailbox else "local endpoint")


if __name__ == "__main__":
    agent.run()
