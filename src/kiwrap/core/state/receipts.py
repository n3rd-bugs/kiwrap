"""`Receipt` / `KiwrapError` -> MCP `CallToolResult`.

Shapes (docs/ARCHITECTURE.md §11, verified transport behaviour in docs/VERIFICATION.md claim 11):
- a receipt becomes `TextContent` (a short human summary followed by the JSON) plus an optional
  `ImageContent` (PNG, base64) and `structured_content` = the receipt as JSON (the PNG bytes are
  replaced by `image_info`);
- a teaching error becomes `isError: true` with `KiwrapError.to_dict()` both as text and as
  `structured_content`, so the agent sees `code`, `why`, `fix`, `candidates`, `example` in either
  channel.

The published `outputSchema` of every tool is `ReceiptOut` (pydantic), which mirrors `Receipt`.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import asdict
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, Field

from kiwrap.core.state import KiwrapError, Receipt

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_TEXT_CHARS = 60_000


class ValidatorDeltaOut(BaseModel):
    name: str
    before: int | None = None
    after: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ImageInfo(BaseModel):
    width: int | None = None
    height: int | None = None
    bytes: int
    mime_type: str = "image/png"


class ReceiptOut(BaseModel):
    """JSON shape of every kw_* result (`structuredContent`)."""

    ok: bool = True
    changed: dict[str, list[str]] = Field(
        default_factory=lambda: {"added": list[str](), "updated": [], "removed": []}
    )
    validators: list[ValidatorDeltaOut] = Field(default_factory=list)
    provider_used: str = ""
    degraded: list[str] = Field(default_factory=list)
    snapshot: str | None = None
    stage: str | None = None
    next_suggested: str | None = None
    warnings: list[str] = Field(default_factory=list)
    image_info: ImageInfo | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ErrorOut(BaseModel):
    """`KiwrapError.to_dict()` (the `structuredContent` of an `isError` result)."""

    code: str
    message: str
    why: str = ""
    fix: str = ""
    candidates: list[str] = Field(default_factory=list)
    example: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width/height from the IHDR chunk (the first chunk of every PNG)."""
    if len(data) >= 24 and data[:8] == PNG_SIGNATURE and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return None


def receipt_to_dict(receipt: Receipt, *, ok: bool = True) -> dict[str, Any]:
    raw = asdict(receipt)
    image = raw.pop("image", None)
    info = None
    if image:
        size = png_size(image)
        info = {
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "bytes": len(image),
            "mime_type": "image/png",
        }
    return {"ok": ok, **raw, "image_info": info}


def _summary_line(tool: str, receipt: Receipt, ok: bool) -> str:
    bits = [f"{tool}: {'ok' if ok else 'NOT ok'}"]
    if receipt.stage:
        bits.append(f"stage={receipt.stage}")
    if receipt.provider_used:
        bits.append(f"provider={receipt.provider_used}")
    if receipt.snapshot:
        bits.append(f"snapshot={receipt.snapshot[:12]}")
    if receipt.degraded:
        bits.append("degraded=" + ",".join(receipt.degraded))
    if receipt.next_suggested:
        bits.append(f"next={receipt.next_suggested}")
    return "  ".join(bits)


def receipt_to_result(
    tool: str, receipt: Receipt, *, ok: bool = True, text: str | None = None
) -> CallToolResult:
    structured = receipt_to_dict(receipt, ok=ok)
    body = text if text is not None else json.dumps(structured, indent=1, default=str)
    if len(body) > MAX_TEXT_CHARS:
        body = body[:MAX_TEXT_CHARS] + (
            "\n... (text truncated mid-JSON: the full payload is in structuredContent; re-call with a smaller "
            "limit, detail='summary' or filters, or follow next_cursor)"
        )
    content: list[Any] = [TextContent(type="text", text=_summary_line(tool, receipt, ok) + "\n" + body)]
    if receipt.image:
        content.append(
            ImageContent(
                type="image", data=base64.b64encode(receipt.image).decode("ascii"), mime_type="image/png"
            )
        )
    return CallToolResult(content=content, structured_content=structured, is_error=False)


def error_to_result(err: KiwrapError) -> CallToolResult:
    payload = err.to_dict()
    lines = [f"{err.code}: {err.message}"]
    if err.why:
        lines.append(f"why: {err.why}")
    if err.fix:
        lines.append(f"fix: {err.fix}")
    if err.candidates:
        lines.append("candidates: " + ", ".join(err.candidates[:20]))
    if err.example:
        lines.append(f"example: {err.example}")
    lines.append(json.dumps(payload, default=str))
    return CallToolResult(
        content=[TextContent(type="text", text="\n".join(lines))], structured_content=payload, is_error=True
    )
