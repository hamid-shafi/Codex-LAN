"""CODEX: Shared validation for file attachments and generated image artifacts."""

from __future__ import annotations

import re


# CODEX: User uploads travel over HTTP and retain the application's existing 25 MiB allowance.
MAX_USER_IMAGE_BYTES = 25 * 1024 * 1024
MAX_USER_FILE_BYTES = MAX_USER_IMAGE_BYTES
# CODEX: Generated-image base64 plus JSON framing must remain below the native 8 MiB stdout ceiling.
MAX_GENERATED_IMAGE_BYTES = 5 * 1024 * 1024


def attachment_name(name: str) -> str:
    """CODEX: Keep an inert display name, never an uploaded path or control sequence."""
    value = str(name or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(character for character in value if character.isprintable()).strip()[:200]
    return value if value not in {"", ".", ".."} else "attachment"


def attachment_mime(data: bytes) -> str:
    """CODEX: Only verified raster images may render inline; all other files download."""
    try:
        return image_mime(data)
    except ValueError:
        return "application/octet-stream"


def attachment_extension(name: str, mime: str) -> str:
    """CODEX: Retain a short inert file suffix without accepting paths or shell syntax."""
    if mime.startswith("image/"):
        return image_extension(mime)
    suffix = attachment_name(name).rsplit(".", 1)
    return "." + suffix[-1].lower() if len(suffix) == 2 and re.fullmatch(r"[A-Za-z0-9]{1,12}", suffix[-1]) else ".bin"


def image_mime(data: bytes) -> str:
    """CODEX: Identify only the browser-safe raster formats accepted by the application."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Please use a PNG, JPEG, GIF, or WebP image.")


def image_extension(mime: str) -> str:
    """CODEX: Return a fixed extension for a previously validated image MIME type."""
    try:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }[mime]
    except KeyError as exc:
        raise ValueError("Unsupported image MIME type.") from exc
