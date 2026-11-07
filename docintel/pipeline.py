from __future__ import annotations

import hashlib
import time
from typing import Any

try:
    from .config import Settings, get_settings
    from .extract import DOC_TYPES, Extraction, classify, extract
    from .llm import GeminiFiller
    from .mineru import MinerUClient
    from .text import to_plain_text
    from .validate import summarize, validate
except ImportError:
    from config import Settings, get_settings
    from extract import DOC_TYPES, Extraction, classify, extract
    from llm import GeminiFiller
    from mineru import MinerUClient
    from text import to_plain_text
    from validate import summarize, validate


def _public_fields(ex: Extraction) -> dict[str, Any]:
    return {k: {"value": f.value, "confidence": round(f.confidence, 2), "source": f.source} for k, f in ex.fields.items()}


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def analyze_markdown(
    markdown: str,
    filename: str = "document",
    document_type: str | None = None,
    settings: Settings | None = None,
    registry: dict[str, str] | None = None,
    parse_source: str = "markdown",
    document_id: str | None = None,
    parse_ms: int | None = None,
) -> dict[str, Any]:
    """Classify, extract and validate already-parsed Markdown."""
    settings = settings or get_settings()
    started = time.perf_counter()
    document_id = document_id or "doc_" + hashlib.sha256(markdown.encode()).hexdigest()[:10]

    if document_type and document_type not in DOC_TYPES:
        raise ValueError(f"document_type must be one of {', '.join(DOC_TYPES)}")

    mark = time.perf_counter()
    detected, confidence = classify(to_plain_text(markdown))
    classify_ms = _ms_since(mark)
    doc_type = document_type or detected

    mark = time.perf_counter()
    ex = extract(doc_type, markdown)
    extract_ms = _ms_since(mark)

    mark = time.perf_counter()
    filled = GeminiFiller(settings).fill(ex, markdown)
    llm_ms = _ms_since(mark)

    mark = time.perf_counter()
    checks = validate(ex, settings.review_confidence_threshold, registry, document_id)
    validate_ms = _ms_since(mark)
    validation = summarize(checks, doc_type)

    stages_ms: dict[str, int] = {
        "classify": classify_ms,
        "extract": extract_ms,
        "llm": llm_ms,
        "validate": validate_ms,
    }
    if parse_ms is not None:
        stages_ms["parse"] = parse_ms

    result: dict[str, Any] = {
        "document_id": document_id,
        "filename": filename,
        "document_type": doc_type,
        "classification": {"type": detected, "confidence": confidence, "overridden": bool(document_type and document_type != detected)},
        "fields": _public_fields(ex),
        "line_items": ex.line_items,
        "validation": {**validation, "checks": checks},
        "needs_human_review": validation["status"] == "needs_review",
        "processing": {
            "parser": parse_source,
            "llm_filled": filled,
            "duration_ms": (parse_ms or 0) + _ms_since(started),
            "stages_ms": stages_ms,
        },
    }
    if ex.extras.get("clauses"):
        result["clauses"] = ex.extras["clauses"]
    return result


def analyze_document(
    data: bytes,
    filename: str,
    document_type: str | None = None,
    settings: Settings | None = None,
    registry: dict[str, str] | None = None,
    mineru: MinerUClient | None = None,
) -> dict[str, Any]:
    """Full pipeline: MinerU parse (PDF/image/Office) -> classify -> extract -> validate."""
    settings = settings or get_settings()
    mark = time.perf_counter()
    parsed = (mineru or MinerUClient(settings)).parse(data, filename)
    parse_ms = _ms_since(mark)
    return analyze_markdown(
        parsed.markdown,
        filename,
        document_type,
        settings,
        registry,
        parse_source=parsed.source,
        document_id="doc_" + hashlib.sha256(data).hexdigest()[:10],
        parse_ms=parse_ms,
    )
