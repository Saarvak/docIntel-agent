from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import requests

from .config import Settings

TEXT_EXTENSIONS = {"md", "markdown", "txt"}


class MinerUError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedDocument:
    markdown: str
    content_list: list[dict[str, Any]] | None
    source: str  # "mineru_api" | "offline_cache" | "text"


class MinerUClient:
    """Sends documents to the MinerU `/file_parse` API and returns Markdown (+ JSON content list)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension in TEXT_EXTENSIONS:
            return ParsedDocument(data.decode("utf-8", errors="ignore"), None, "text")

        try:
            return self._call_api(data, filename)
        except MinerUError:
            cached = self._offline_lookup(data) if self.settings.mineru_offline_fallback else None
            if cached is not None:
                return cached
            raise

    def health(self) -> bool:
        try:
            return requests.get(f"{self.settings.mineru_api_url}/docs", timeout=3).status_code < 500
        except requests.RequestException:
            return False

    def _call_api(self, data: bytes, filename: str) -> ParsedDocument:
        url = f"{self.settings.mineru_api_url}/file_parse"
        try:
            response = requests.post(
                url,
                files={"files": (filename, data, "application/pdf" if filename.lower().endswith(".pdf") else "application/octet-stream")},
                data={
                    "return_md": "true",
                    "return_content_list": "true",
                    "return_images": "false",
                    "backend": self.settings.mineru_backend,
                    "lang_list": self.settings.mineru_lang,
                },
                timeout=self.settings.mineru_timeout,
            )
        except requests.RequestException as exc:
            raise MinerUError(
                f"Could not reach MinerU at {url}. Start it with `mineru-api --host 0.0.0.0 --port 8000`."
            ) from exc

        if response.status_code >= 400:
            raise MinerUError(f"MinerU returned HTTP {response.status_code}: {response.text[-500:]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MinerUError("MinerU returned a non-JSON response") from exc

        markdown = _find_first(payload, {"md_content", "md", "markdown"})
        if not isinstance(markdown, str) or not markdown.strip():
            raise MinerUError("MinerU response did not contain Markdown (md_content)")

        content_list = _find_first(payload, {"content_list"})
        if isinstance(content_list, str):
            try:
                content_list = json.loads(content_list)
            except ValueError:
                content_list = None
        return ParsedDocument(markdown.strip(), content_list if isinstance(content_list, list) else None, "mineru_api")

    def _offline_lookup(self, data: bytes) -> ParsedDocument | None:
        """Use pre-parsed MinerU Markdown stored next to a sample PDF with identical bytes."""
        samples = self.settings.samples_dir
        if not samples.exists():
            return None
        digest = hashlib.sha256(data).hexdigest()
        for pdf in samples.glob("*.pdf"):
            markdown_path = pdf.with_suffix(".md")
            if markdown_path.exists() and hashlib.sha256(pdf.read_bytes()).hexdigest() == digest:
                return ParsedDocument(markdown_path.read_text(encoding="utf-8"), None, "offline_cache")
        return None


def _find_first(payload: Any, keys: set[str]) -> Any:
    if isinstance(payload, dict):
        for key in keys:
            if payload.get(key):
                return payload[key]
        for value in payload.values():
            found = _find_first(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_first(item, keys)
            if found:
                return found
    return None
