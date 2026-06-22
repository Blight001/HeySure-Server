"""Outbound text normalization shared by bot adapters.

``normalize_qq_text`` was byte-for-byte ``normalize_feishu_text`` (strip path)
minus the two Markdown-table handling lines. The common Markdown→plain pipeline
lives here once; each channel only chooses whether to collapse tables.
"""

from __future__ import annotations

import re


def strip_markdown_to_plain(text: str, *, collapse_tables: bool) -> str:
    """Convert Markdown into readable plain text.

    Keeps the content (link labels, list text, headings) while removing the
    formatting punctuation that would otherwise leak to chat users. When
    ``collapse_tables`` is set, Markdown table separator rows are dropped and
    pipe-delimited cells are flattened into spaces (Feishu); QQ skips that.
    """
    body = str(text or "")
    if not body:
        return ""

    body = body.replace("\r\n", "\n").replace("\r", "\n")

    # Links / images: keep the label, drop the URL and image marker.
    body = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", body)
    body = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", body)

    # Fenced code keeps content, drops fence punctuation and optional language.
    body = re.sub(r"```[^\n]*\n?", "", body)
    body = body.replace("```", "")

    lines = []
    for raw_line in body.split("\n"):
        line = raw_line.rstrip()
        if collapse_tables:
            stripped = line.strip()
            if re.fullmatch(r"[:\-\s|]+", stripped) and "|" in stripped:
                continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s{0,3}>\s?", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        if collapse_tables and "|" in line:
            line = re.sub(r"\s*\|\s*", "  ", line).strip()
        lines.append(line)
    body = "\n".join(lines)

    # Inline emphasis marks: remove the punctuation, keep text.
    body = re.sub(r"(?<!\w)([*_~]{1,3})(\S(?:.*?\S)?)\1(?!\w)", r"\2", body)
    body = body.replace("`", "")

    # Markdown task checkboxes and escaped punctuation.
    body = re.sub(r"\[\s*[xX ]\s*\]\s*", "", body)
    body = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>])", r"\1", body)

    # Avoid symbols stuck to CJK/ASCII after marker removal.
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{2,}", "\n", body)
    return body.strip()
