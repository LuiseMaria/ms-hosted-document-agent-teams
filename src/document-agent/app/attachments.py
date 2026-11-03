"""Resolve inline documents and project-scoped Foundry file references."""

from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass

from agent_framework import Content
from openai import APIError, OpenAI

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_OCR_MEDIA_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/tiff",
    "image/bmp", "image/heif",
}


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes
    media_type: str


class AttachmentResolver:
    """Download file IDs using the configured project's authenticated Files API.

    Ordinary URLs are deliberately not fetched. This avoids turning user input
    into arbitrary server-side HTTP requests.
    """

    def __init__(self, client: OpenAI | None = None) -> None:
        self._client = client

    def resolve(self, content: Content, fallback_name: str) -> Attachment:
        name = content.name or content.additional_properties.get("filename")
        if not isinstance(name, str) or not name.strip():
            name = None
        else:
            name = name.strip()
        media_type = content.media_type or ""
        try:
            if content.file_id:
                if self._client is None:
                    raise ValueError("Foundry file downloads are not configured.")
                metadata = self._client.files.retrieve(content.file_id, timeout=60)
                if metadata.bytes > MAX_ATTACHMENT_BYTES:
                    raise ValueError("Attachment exceeds the 20 MiB file limit.")
                name = name or metadata.filename
                data = bytearray()
                with self._client.files.with_streaming_response.content(
                    content.file_id, timeout=60
                ) as response:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        if len(data) + len(chunk) > MAX_ATTACHMENT_BYTES:
                            raise ValueError("Attachment exceeds the 20 MiB file limit.")
                        data.extend(chunk)
                raw = bytes(data)
            elif content.uri and content.uri.startswith("data:"):
                header, separator, encoded = content.uri.partition(",")
                if not separator or not header.endswith(";base64"):
                    raise ValueError("Attachment must use a base64 data URI.")
                if len(encoded) > 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3):
                    raise ValueError("Attachment exceeds the 20 MiB file limit.")
                raw = base64.b64decode(encoded, validate=True)
                media_type = media_type or header[5:-7]
            elif content.uri:
                raise ValueError(
                    "External file URLs are not enabled. Send file_data or a Foundry file_id."
                )
            else:
                raise ValueError("Attachment has neither inline data nor a Foundry file_id.")

            if not raw:
                raise ValueError("Attachment is empty.")
            if len(raw) > MAX_ATTACHMENT_BYTES:
                raise ValueError("Attachment exceeds the 20 MiB file limit.")
            name = name or fallback_name
            media_type = media_type.split(";", 1)[0].strip().lower()
            if media_type in {"", "application/octet-stream"}:
                media_type = mimetypes.guess_type(name)[0] or ""
                if not media_type and raw.startswith(b"%PDF-"):
                    media_type = "application/pdf"
            media_type = {"image/jpg": "image/jpeg"}.get(media_type, media_type)
            if media_type not in _OCR_MEDIA_TYPES:
                raise ValueError("Unsupported attachment type. Send a PDF or a supported image.")
        except ValueError:
            logger.error("Attachment rejected: invalid payload, size, type or source.")
            raise
        except APIError as exc:
            logger.error("Foundry file retrieval failed.")
            raise RuntimeError(
                "Could not load the Foundry file. Check its project, permissions and availability."
            ) from exc

        logger.info("Attachment resolved | media_type=%s bytes=%d", media_type, len(raw))
        return Attachment(name=name, data=raw, media_type=media_type)
