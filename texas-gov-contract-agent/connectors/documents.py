"""Download solicitation attachments and pull out their text so the agents can read requirements.

Supports PDF (pypdf), DOCX (built-in zip/XML read), and plain text/HTML. Size-capped and best-effort:
a scanned PDF with no text layer simply contributes nothing.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile

from core import http
from core.extract import clean, html_to_text

log = logging.getLogger(__name__)


def extract_attachments(attachments: list[dict], max_files: int = 3, max_mb: float = 8,
                        max_chars: int = 30000, extra_params: dict | None = None) -> tuple[str, list[dict]]:
    """Returns (combined_text, per-file status list)."""
    texts, report = [], []
    budget = max_chars
    for att in attachments[:max_files]:
        url = att.get("url")
        if not url or budget <= 0:
            continue
        status = {"name": att.get("name", url), "url": url, "chars": 0, "note": ""}
        try:
            resp = http.get(url, params=extra_params or None, stream=True, timeout=60)
            if resp.status_code != 200:
                status["note"] = f"HTTP {resp.status_code}"
                report.append(status)
                continue
            data = _read_capped(resp, max_mb)
            if data is None:
                status["note"] = f"skipped (over {max_mb} MB)"
                report.append(status)
                continue
            ctype = (resp.headers.get("Content-Type") or "").lower()
            disp = resp.headers.get("Content-Disposition") or ""
            name_match = re.search(r'filename="?([^";]+)', disp)
            if name_match:
                status["name"] = name_match.group(1)
            text = bytes_to_text(data, ctype, status["name"])
            text = clean(text)[:budget]
            budget -= len(text)
            status["chars"] = len(text)
            if text:
                texts.append(f"[{status['name']}]\n{text}")
            else:
                status["note"] = "no readable text (may be scanned)"
        except Exception as exc:
            status["note"] = f"failed: {exc.__class__.__name__}"
        report.append(status)
    return "\n\n".join(texts), report


def _read_capped(resp, max_mb: float) -> bytes | None:
    limit = int(max_mb * 1024 * 1024)
    buf = io.BytesIO()
    for chunk in resp.iter_content(64 * 1024):
        buf.write(chunk)
        if buf.tell() > limit:
            return None
    return buf.getvalue()


def bytes_to_text(data: bytes, content_type: str = "", name: str = "") -> str:
    name = (name or "").lower()
    if data[:4] == b"%PDF" or "pdf" in content_type or name.endswith(".pdf"):
        return _pdf_text(data)
    if data[:2] == b"PK" and (name.endswith(".docx") or "word" in content_type or _is_docx(data)):
        return _docx_text(data)
    if "html" in content_type or data.lstrip()[:1] == b"<":
        return html_to_text(data.decode("utf-8", "ignore"))
    if "text" in content_type or name.endswith((".txt", ".csv")):
        return data.decode("utf-8", "ignore")
    return ""


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:60])
    except Exception as exc:
        log.info("PDF text extraction failed: %s", exc)
        return ""


def _is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return "word/document.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    except Exception:
        return ""
