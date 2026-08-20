"""QQ markdown normalization helpers."""
from __future__ import annotations
import re
from typing import Any, Dict
from ..text_format import strip_markdown_to_plain

def normalize_qq_text(text: str) -> str:
    return strip_markdown_to_plain(text, collapse_tables=False)


def _prepare_markdown_text(text: str) -> str:
    """Light cleanup that *keeps* markdown syntax (unlike ``normalize_qq_text``).

    QQ native markdown renders the body as-is, so we only normalize line
    endings and collapse runs of blank lines — headings, lists, links, code
    fences, emphasis, etc. are preserved.
    """
    body = str(text or "")
    if not body:
        return ""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _prepare_stream_markdown_text(text: str) -> str:
    """Normalize line endings without stripping incremental whitespace."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _qq_markdown_field(content: str, markdown_mode: str, template_id: str) -> Dict[str, Any]:
    """Build the ``markdown`` object for a ``msg_type=2`` message.

    Two mutually exclusive shapes (per QQ open-platform spec):
      - native:   ``{"content": "<raw markdown>"}``
      - template: ``{"custom_template_id": "<id>", "params": [{key, values}]}``

    Template mode assumes the approved template exposes a single ``content``
    placeholder; bots with multi-field templates should send via the explicit
    MCP tool instead.
    """
    mode = str(markdown_mode or "native").strip().lower()
    tpl = str(template_id or "").strip()
    if mode == "template" and tpl:
        return {
            "custom_template_id": tpl,
            "params": [{"key": "content", "values": [content]}],
        }
    return {"content": content}


