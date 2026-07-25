"""Parse and apply Aider-style SEARCH/REPLACE edit blocks.

Pure, no LLM dependency. Used by the refine step to apply targeted edits
instead of regenerating the whole HTML file.
"""

import re
from collections import namedtuple

EditBlock = namedtuple("EditBlock", ["search", "replace"])

_BLOCK_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> REPLACE",
    re.DOTALL,
)


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_edit_blocks(text: str) -> list:
    """Extract SEARCH/REPLACE blocks, tolerating surrounding prose/fences.

    Normalizes CRLF->LF and deduplicates identical (search, replace) pairs.
    Rejects blocks whose search or replace body contains a bare divider line
    (<<<<<<< SEARCH, =======, or >>>>>>> REPLACE).
    Returns [] when no well-formed block is present.
    """
    text = _lf(text)
    blocks, seen = [], set()
    dividers = {"<<<<<<< SEARCH", "=======", ">>>>>>> REPLACE"}
    for m in _BLOCK_RE.finditer(text):
        block = EditBlock(m.group(1), m.group(2))
        # Reject if search or replace contains a bare divider line (a mis-parse).
        # Note: a rejected block is dropped, not signalled — if other blocks in the
        # same response are valid, refine_code applies them and stores the result
        # without falling back, so the dropped block's edit is silently skipped
        # (the next audit iteration re-catches the unfixed issue). Acceptable
        # because bare 7-char divider lines effectively never occur in HTML.
        if any(line in dividers for line in block.search.split("\n")) or any(line in dividers for line in block.replace.split("\n")):
            continue
        if block not in seen:
            seen.add(block)
            blocks.append(block)
    return blocks


def _normalize_line(line: str) -> str:
    return " ".join(line.split())


def apply_edits(code: str, blocks: list):
    """Apply blocks to `code` in order, each against the progressively-edited
    result. A block is unmatched (code left untouched for it) when SEARCH is
    empty/whitespace-only, occurs 2+ times, or matches in neither pass.

    Returns (new_code, unmatched_blocks). Store new_code only when unmatched
    is empty.
    """
    code = _lf(code)
    unmatched = []
    for block in blocks:
        search = block.search
        if not search.strip():
            unmatched.append(block)
            continue
        # 1. exact substring — must be unique
        first = code.find(search)
        if first != -1:
            if code.find(search, first + 1) != -1:
                unmatched.append(block)  # ambiguous
                continue
            code = code[:first] + block.replace + code[first + len(search) :]
            continue
        # 2. line-windowed whitespace-normalized — must be unique
        code_lines = code.split("\n")
        search_lines = search.split("\n")
        norm_code = [_normalize_line(x) for x in code_lines]
        norm_search = [_normalize_line(x) for x in search_lines]
        w = len(norm_search)
        hits = [i for i in range(len(norm_code) - w + 1) if norm_code[i : i + w] == norm_search]
        if len(hits) == 1:
            i = hits[0]
            code = "\n".join(code_lines[:i] + block.replace.split("\n") + code_lines[i + w :])
            continue
        unmatched.append(block)  # zero or ambiguous
    return code, unmatched
