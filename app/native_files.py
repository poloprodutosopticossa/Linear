"""Bounded, best-effort CRM file transfers to Linear's private storage."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from email.message import Message
import hashlib
import logging
import re
import uuid
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)
CRM_FILE_FIELD = "ufCrm_1662137274943"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024
MAX_FILES = 8
TRANSFER_SECONDS = 45
_private_request = ContextVar("private_http_request", default=False)


class _PrivateHTTPFilter(logging.Filter):
    def filter(self, record):
        # HTTPX logs full URLs; HTTPCore debug logs may include signed headers.
        return not _private_request.get()


for _name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
              "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(_name).addFilter(_PrivateHTTPFilter())


@contextmanager
def private_http_logs():
    token = _private_request.set(True)
    try:
        yield
    finally:
        _private_request.reset(token)


def issue_title(deal, fields):
    """Use only a uniquely identified string field labelled Assunto by the portal."""
    candidates = []
    for name, meta in fields.items():
        if not re.fullmatch(r"UF_CRM_[A-Z0-9_]+", name) or not isinstance(meta, dict):
            continue
        labels = [meta.get(key) for key in ("title", "formLabel", "listLabel")]
        labels = [label for value in labels for label in
                  (value.values() if isinstance(value, dict) else [value])]
        if any(isinstance(label, str) and label.strip().casefold() == "assunto" for label in labels):
            candidates.append((name, meta))
    title = str(deal.get("TITLE") or f"Negócio Bitrix24 #{deal['ID']}").strip()
    if len(candidates) != 1:
        logger.warning("CRM subject field unavailable or ambiguous; using deal title")
        return title
    name, meta = candidates[0]
    subject = deal.get(name)
    if meta.get("type") != "string" or meta.get("isMultiple") not in (False, None):
        logger.warning("CRM subject field has unsupported type; using deal title")
        return title
    if not isinstance(subject, str) or not subject.strip():
        return title
    subject = subject.strip()
    normalized = lambda value: " ".join(value.casefold().split())
    return title if normalized(subject) in normalized(title) else f"{title} - {subject}"


def _https_url(url):
    if not isinstance(url, str) or any(ord(char) < 33 for char in url) or "\\" in url:
        raise ValueError("invalid_url")
    parsed = urlparse(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError("invalid_url")
    return parsed


def validate_download_url(url, portal_base):
    parsed = _https_url(url)
    portal = _https_url(portal_base)
    if parsed.hostname.lower() != portal.hostname.lower():
        raise ValueError("unauthorized_download")


def _filename(disposition, source_name, file_id):
    message = Message()
    message["Content-Disposition"] = disposition or ""
    name = message.get_filename() or source_name or f"crm-file-{file_id}"
    # Keep the original basename, including RFC 2231/5987 Unicode filenames.
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127).strip()
    return name[:200] or f"crm-file-{file_id}"


async def download_crm_file(client, file, portal_base, remaining):
    url = file.get("urlMachine")
    validate_download_url(url, portal_base)
    limit = min(MAX_FILE_BYTES, remaining)
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
        if response.status_code != 200:
            raise ValueError("download_http")  # Includes all redirects: never follow them.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("encoded_download")
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if (not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", content_type)
                or content_type in ("text/html", "application/xhtml+xml", "image/svg+xml")
                or "json" in content_type):
            raise ValueError("download_content_type")
        length = response.headers.get("content-length")
        if length and (int(length) < 0 or int(length) > limit):
            raise ValueError("file_too_large")
        buffer = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            if len(buffer) + len(chunk) > limit:
                raise ValueError("file_too_large")
            buffer.extend(chunk)
        prefix = bytes(buffer).lstrip(b"\xef\xbb\xbf \t\r\n")[:512].lower()
        if (not buffer or prefix.startswith((b"{", b"[", b"<"))):
            raise ValueError("download_error_body")
        if length and int(length) != len(buffer):
            raise ValueError("download_length")
        name = _filename(response.headers.get("content-disposition"), file.get("name"), file["id"])
        return bytes(buffer), content_type, name


async def upload_and_attach(client, linear_request, issue_id, file_id, content, mime, name):
    result = await linear_request("""
        mutation($contentType: String!, $filename: String!, $size: Int!) {
          fileUpload(contentType: $contentType, filename: $filename, size: $size) {
            success uploadFile { uploadUrl assetUrl headers { key value } }
          }
        }
    """, {"contentType": mime, "filename": name, "size": len(content)})
    payload = result["fileUpload"]
    if payload.get("success") is not True:
        raise ValueError("upload_not_ready")
    upload = payload["uploadFile"]
    _https_url(upload["uploadUrl"])
    _https_url(upload["assetUrl"])
    if upload["assetUrl"] == upload["uploadUrl"] or urlparse(upload["assetUrl"]).query:
        raise ValueError("invalid_asset_url")
    headers = httpx.Headers({"Content-Type": mime, "Cache-Control": "public, max-age=31536000"})
    headers.update({header["key"]: header["value"] for header in upload["headers"]})
    response = await client.put(upload["uploadUrl"], content=content, headers=headers)
    if not response.is_success:
        raise ValueError("upload_http")
    # Stable attachment identity for this issue + CRM file; repeated entries do not
    # create distinct attachments even if Linear returns a new storage URL.
    identity = f"bitrix-crm-file-v1:{issue_id}:{CRM_FILE_FIELD}:{file_id}"
    attachment_id = str(uuid.UUID(bytes=hashlib.sha256(identity.encode()).digest()[:16], version=4))
    result = await linear_request("""
        mutation($input: AttachmentCreateInput!) {
          attachmentCreate(input: $input) { success attachment { id } }
        }
    """, {"input": {"id": attachment_id, "issueId": issue_id, "title": name,
                   "url": upload["assetUrl"]}})
    created = result["attachmentCreate"]
    if (created.get("success") is not True
            or (created.get("attachment") or {}).get("id") != attachment_id):
        raise ValueError("attachment_not_confirmed")


async def copy_deal_attachments(deal_id, issue_id, portal_base, bitrix_request, linear_request):
    report = {"attached": 0, "failed": 0, "skipped": 0}
    stage = "metadata"
    async def transfer():
        nonlocal stage
        with private_http_logs():
            item = (await bitrix_request("crm.item.get", {
                "entityTypeId": 2, "id": deal_id,
            }))["result"]["item"]
            if str(item.get("id")) != str(deal_id):
                raise ValueError("invalid_deal")
            files = item.get(CRM_FILE_FIELD) or []
            if isinstance(files, dict):
                files = [files]
            if not isinstance(files, list):
                raise ValueError("invalid_files")
            seen = set()
            total = 0
            attempted = 0
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5),
                                         follow_redirects=False, trust_env=False) as client:
                for file in files:
                    stage = "transfer"
                    content = None
                    try:
                        file_id = int(file["id"])
                        if file_id <= 0:
                            raise ValueError("invalid_file_id")
                        if file_id in seen:
                            continue
                        seen.add(file_id)
                        if attempted >= MAX_FILES or total >= MAX_TOTAL_BYTES:
                            report["skipped"] += 1
                            continue
                        attempted += 1
                        content, mime, name = await download_crm_file(
                            client, file, portal_base, MAX_TOTAL_BYTES - total)
                        total += len(content)
                        stage = "upload"
                        await upload_and_attach(client, linear_request, issue_id,
                                                file_id, content, mime, name)
                        report["attached"] += 1
                    except Exception:
                        # Never log exception text, URLs, headers, filenames or API bodies.
                        report["failed"] += 1
                        logger.warning("CRM attachment failed (stage=%s)", stage)
                    finally:
                        content = None
    try:
        await asyncio.wait_for(transfer(), timeout=TRANSFER_SECONDS)
    except asyncio.TimeoutError:
        report["failed"] += 1
        logger.warning("CRM attachment budget exhausted")
    except Exception:
        report["failed"] += 1
        logger.warning("CRM attachment processing failed (stage=%s)", stage)
    return report
