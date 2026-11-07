"""MinerU cloud API client. Parsing runs on mineru.net — nothing is started on this machine."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import zipfile
from typing import Any

import requests

try:
    from .config import Settings
except ImportError:
    from config import Settings

logger = logging.getLogger(__name__)

TEXT_EXTENSIONS = {"md", "markdown", "txt"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "jp2", "webp", "gif", "bmp"}
POLL_SECONDS = 2.0


class MinerUError(RuntimeError):
    pass


class ParsedDocument:
    def __init__(self, markdown: str, content_list: list[dict[str, Any]] | None, source: str) -> None:
        self.markdown = markdown
        self.content_list = content_list
        self.source = source  # "mineru_api" | "text"


class MinerUClient:
    """Uploads a file to the hosted MinerU API and returns Markdown (+ JSON content list)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def base_url(self) -> str:
        return self.settings.mineru_api_url.rstrip("/")

    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension in TEXT_EXTENSIONS:
            return ParsedDocument(data.decode("utf-8", errors="ignore"), None, "text")

        logger.info("MinerU: parsing %s (%s bytes) via %s", filename, len(data), self.base_url)
        return self._call_api(data, filename, extension)

    def health(self) -> bool:
        try:
            return requests.get(self.base_url, timeout=5).status_code < 500
        except requests.RequestException:
            return False

    def _call_api(self, data: bytes, filename: str, extension: str) -> ParsedDocument:
        if self.settings.mineru_api_token:
            return self._parse_v4(data, filename, extension)
        return self._parse_agent(data, filename, extension)

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.mineru_api_token:
            headers["Authorization"] = f"Bearer {self.settings.mineru_api_token}"
        return headers

    def _timeout(self) -> tuple[float, int]:
        return (3, self.settings.mineru_timeout)

    def _ocr(self, extension: str) -> bool:
        if self.settings.mineru_ocr is not None:
            return self.settings.mineru_ocr
        return extension in IMAGE_EXTENSIONS

    def _parse_v4(self, data: bytes, filename: str, extension: str) -> ParsedDocument:
        apply = f"{self.base_url}/api/v4/file-urls/batch"
        payload = {
            "files": [{"name": filename, "data_id": hashlib.sha256(data).hexdigest()[:32], "is_ocr": self._ocr(extension)}],
            "model_version": self.settings.mineru_backend,
            "language": self.settings.mineru_lang,
            "enable_table": True,
            "enable_formula": False,
        }
        logger.info("MinerU: requesting v4 upload URL")
        try:
            response = requests.post(apply, headers={**self._auth_headers(), "Content-Type": "application/json"}, json=payload, timeout=self._timeout())
        except requests.RequestException as exc:
            raise MinerUError(f"Could not reach MinerU API at {apply}.") from exc

        body = _api_json(response)
        upload_urls = body.get("file_urls") or []
        batch_id = body.get("batch_id")
        if not batch_id or not upload_urls:
            raise MinerUError("MinerU API did not return an upload URL. Check MINERU_API_TOKEN.")

        self._put_file(upload_urls[0], data)
        result = self._poll(
            f"{self.base_url}/api/v4/extract-results/batch/{batch_id}",
            done=lambda item: item.get("state") == "done",
            failed=lambda item: item.get("state") == "failed",
            item=lambda data: (data.get("extract_result") or [{}])[0],
        )
        zip_url = result.get("full_zip_url")
        if not zip_url:
            raise MinerUError("MinerU finished but returned no result zip.")
        return _from_zip(self._download(zip_url), "mineru_api")

    def _parse_agent(self, data: bytes, filename: str, extension: str) -> ParsedDocument:
        apply = f"{self.base_url}/api/v1/agent/parse/file"
        payload = {
            "file_name": filename,
            "language": self.settings.mineru_lang,
            "enable_table": True,
            "enable_formula": False,
            "is_ocr": self._ocr(extension),
        }
        logger.info("MinerU: requesting agent upload URL")
        try:
            response = requests.post(apply, json=payload, timeout=self._timeout())
        except requests.RequestException as exc:
            raise MinerUError(
                f"Could not reach MinerU API at {apply}. "
                "Set MINERU_API_TOKEN from https://mineru.net/apiManage/token (no local MinerU process)."
            ) from exc

        body = _api_json(response)
        task_id = body.get("task_id")
        file_url = body.get("file_url")
        if not task_id or not file_url:
            raise MinerUError("MinerU agent API did not return task_id/file_url.")

        self._put_file(file_url, data)
        result = self._poll(
            f"{self.base_url}/api/v1/agent/parse/{task_id}",
            done=lambda item: item.get("state") == "done",
            failed=lambda item: item.get("state") == "failed",
            item=lambda data: data,
        )
        markdown_url = result.get("markdown_url")
        if not markdown_url:
            raise MinerUError("MinerU finished but returned no markdown_url.")
        markdown = self._download(markdown_url).decode("utf-8", errors="replace")
        if not markdown.strip():
            raise MinerUError("MinerU Markdown was empty.")
        return ParsedDocument(markdown.strip(), None, "mineru_api")

    def _put_file(self, url: str, data: bytes) -> None:
        # Presigned OSS URLs are signed with an empty Content-Type. requests would
        # otherwise send application/octet-stream and OSS returns 403.
        logger.info("MinerU: uploading %s bytes", len(data))
        try:
            response = requests.put(url, data=data, timeout=(15, 120), headers={"Content-Type": ""})
        except requests.RequestException as exc:
            raise MinerUError(f"Could not upload the file to MinerU storage: {exc}") from exc
        if response.status_code >= 400:
            raise MinerUError(f"MinerU file upload returned HTTP {response.status_code}: {response.text[-300:]}")
        logger.info("MinerU: upload HTTP %s", response.status_code)

    def _poll(self, url: str, done, failed, item) -> dict[str, Any]:
        deadline = time.monotonic() + self.settings.mineru_timeout
        headers = self._auth_headers()
        last_state = "unknown"
        logger.info("MinerU: polling %s", url)
        while time.monotonic() < deadline:
            try:
                response = requests.get(url, headers=headers, timeout=self._timeout())
            except requests.RequestException as exc:
                raise MinerUError(f"Could not poll MinerU task at {url}.") from exc
            payload = _api_json(response)
            row = item(payload) or {}
            state = str(row.get("state") or "unknown")
            if state != last_state:
                logger.info("MinerU: task state %s", state)
                last_state = state
            if failed(row):
                raise MinerUError(row.get("err_msg") or f"MinerU task failed ({last_state})")
            if done(row):
                return row
            time.sleep(POLL_SECONDS)
        raise MinerUError(f"MinerU timed out after {self.settings.mineru_timeout}s (last state: {last_state})")

    def _download(self, url: str) -> bytes:
        try:
            response = requests.get(url, timeout=self._timeout())
        except requests.RequestException as exc:
            raise MinerUError(f"Could not download MinerU result: {exc}") from exc
        if response.status_code >= 400:
            raise MinerUError(f"MinerU result download HTTP {response.status_code}")
        return response.content


def _api_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise MinerUError(f"MinerU returned a non-JSON response (HTTP {response.status_code})") from exc
    if response.status_code >= 400:
        raise MinerUError(f"MinerU HTTP {response.status_code}: {str(payload)[:400]}")
    if isinstance(payload, dict) and payload.get("success") is False:
        raise MinerUError(payload.get("msg") or "MinerU authentication failed. Check MINERU_API_TOKEN.")
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0"):
        raise MinerUError(payload.get("msg") or f"MinerU error code {payload.get('code')}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MinerUError("MinerU response was missing data.")
    return data


def _from_zip(blob: bytes, source: str) -> ParsedDocument:
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise MinerUError("MinerU result was not a zip file") from exc
    names = archive.namelist()
    md_name = next((n for n in names if n.endswith("full.md")), None) or next((n for n in names if n.endswith(".md")), None)
    if not md_name:
        raise MinerUError("MinerU zip did not contain Markdown (full.md)")
    markdown = archive.read(md_name).decode("utf-8", errors="replace").strip()
    if not markdown:
        raise MinerUError("MinerU Markdown was empty.")
    content_list = None
    cl_name = next((n for n in names if n.endswith("content_list.json")), None)
    if cl_name:
        try:
            parsed = json.loads(archive.read(cl_name).decode("utf-8", errors="replace"))
            if isinstance(parsed, list):
                content_list = parsed
        except ValueError:
            content_list = None
    return ParsedDocument(markdown, content_list, source)
