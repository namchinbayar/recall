"""Split text into chunks that embed well.

Paragraph-aware: joins paragraphs until a chunk reaches the target size,
then starts a new one, carrying a tail of the previous chunk as overlap so
sentences that straddle a boundary still land in one chunk whole.
"""

from __future__ import annotations

import re

TARGET = 1500      # characters, roughly 350 tokens
OVERLAP = 200


def split(text: str, target: int = TARGET, overlap: int = OVERLAP) -> list[str]:
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        # A single paragraph longer than the target gets hard-split.
        while len(para) > target:
            head, para = para[:target], para[target - overlap :]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)

        if not current:
            current = para
        elif len(current) + 2 + len(para) <= target:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            current = f"{current[-overlap:]}\n\n{para}" if overlap else para

    if current:
        chunks.append(current)
    return chunks
