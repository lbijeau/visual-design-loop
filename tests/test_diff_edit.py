"""Tests for diff_edit: parsing and applying SEARCH/REPLACE blocks (pure, no LLM)."""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diff_edit import EditBlock, apply_edits, parse_edit_blocks


def _block(search, replace):
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


def test_parse_single_block():
    print("  test_parse_single_block...", end=" ")
    blocks = parse_edit_blocks("prose before\n" + _block("old", "new") + "\nprose after")
    assert blocks == [EditBlock("old", "new")]
    print("✅")


def test_parse_ignores_non_blocks_and_dedupes():
    print("  test_parse_ignores_non_blocks_and_dedupes...", end=" ")
    assert parse_edit_blocks("just prose, no blocks") == []
    dup = _block("a", "b") + "\n" + _block("a", "b")
    assert parse_edit_blocks(dup) == [EditBlock("a", "b")]  # deduped
    print("✅")


def test_parse_normalizes_crlf():
    print("  test_parse_normalizes_crlf...", end=" ")
    blocks = parse_edit_blocks(_block("old", "new").replace("\n", "\r\n"))
    assert blocks == [EditBlock("old", "new")]
    print("✅")


def test_apply_exact_single():
    print("  test_apply_exact_single...", end=" ")
    code = "line1\nHELLO\nline3"
    new, unmatched = apply_edits(code, [EditBlock("HELLO", "WORLD")])
    assert new == "line1\nWORLD\nline3"
    assert unmatched == []
    print("✅")


def test_apply_multi_block_sequential():
    print("  test_apply_multi_block_sequential...", end=" ")
    code = "a\nb\nc"
    new, unmatched = apply_edits(code, [EditBlock("a", "X"), EditBlock("X\nb", "Y")])
    assert new == "Y\nc"  # second block matches the first block's output
    assert unmatched == []
    print("✅")


def test_apply_ambiguous_exact_is_unmatched():
    print("  test_apply_ambiguous_exact_is_unmatched...", end=" ")
    code = '<div class="card"></div>\n<div class="card"></div>'
    blocks = [EditBlock('<div class="card"></div>', "CHANGED")]
    new, unmatched = apply_edits(code, blocks)
    assert new == code  # untouched — ambiguous, never guessed
    assert unmatched == blocks
    print("✅")


def test_apply_ambiguous_normalized_is_unmatched():
    print("  test_apply_ambiguous_normalized_is_unmatched...", end=" ")
    # No exact match (differing whitespace), but two lines match after normalization.
    code = "  <div>x</div>  \n\t<div>x</div>\t"
    blocks = [EditBlock("<div>x</div>", "CHANGED")]
    new, unmatched = apply_edits(code, blocks)
    assert new == code  # ambiguous under normalization -> untouched
    assert unmatched == blocks
    print("✅")


def test_apply_empty_search_is_unmatched():
    print("  test_apply_empty_search_is_unmatched...", end=" ")
    code = "before\n\nafter"  # has a blank line the normalized pass could wrongly match
    new, unmatched = apply_edits(code, [EditBlock("   ", "INJECTED")])
    assert new == "before\n\nafter"  # guard fires: no injection at the blank line
    assert unmatched == [EditBlock("   ", "INJECTED")]
    # empty-string search on empty code must also be unmatched, not a position-0 insert
    new2, unmatched2 = apply_edits("", [EditBlock("", "INJECTED")])
    assert new2 == ""
    assert unmatched2 == [EditBlock("", "INJECTED")]
    print("✅")


def test_apply_whitespace_normalized_match():
    print("  test_apply_whitespace_normalized_match...", end=" ")
    code = "before\n    <p>  hi   there </p>\nafter"
    # model reindented / respaced the search text
    blocks = [EditBlock("<p> hi there </p>", "<p>bye</p>")]
    new, unmatched = apply_edits(code, blocks)
    assert unmatched == []
    assert new == "before\n<p>bye</p>\nafter"  # original span (that line) replaced
    print("✅")


def test_apply_unmatched_leaves_code_and_reports():
    print("  test_apply_unmatched_leaves_code_and_reports...", end=" ")
    code = "alpha\nbeta"
    good, bad = EditBlock("alpha", "A"), EditBlock("nonexistent", "Z")
    new, unmatched = apply_edits(code, [good, bad])
    assert new == "A\nbeta"  # good applied
    assert unmatched == [bad]
    print("✅")


def test_parse_rejects_block_with_bare_divider_in_body():
    print("  test_parse_rejects_block_with_bare_divider_in_body...", end=" ")
    # A SEARCH body containing a bare '=======' line must not silently mis-parse
    # into a corrupted (search, replace) pair. Safest correct behavior: no valid
    # block is produced, so the caller routes to the full-file fallback.
    text = "<<<<<<< SEARCH\nline A\n=======\nline B\n=======\nreplacement\n>>>>>>> REPLACE"
    blocks = parse_edit_blocks(text)
    for b in blocks:
        assert "=======" not in b.search.split("\n"), f"search has bare divider: {b.search!r}"
        assert "=======" not in b.replace.split("\n"), f"replace has bare divider: {b.replace!r}"
    print("✅")


if __name__ == "__main__":
    print("\n=== diff_edit Tests ===")
    test_parse_single_block()
    test_parse_ignores_non_blocks_and_dedupes()
    test_parse_normalizes_crlf()
    test_apply_exact_single()
    test_apply_multi_block_sequential()
    test_apply_ambiguous_exact_is_unmatched()
    test_apply_ambiguous_normalized_is_unmatched()
    test_apply_empty_search_is_unmatched()
    test_apply_whitespace_normalized_match()
    test_apply_unmatched_leaves_code_and_reports()
    test_parse_rejects_block_with_bare_divider_in_body()
    print("\nAll tests passed ✅")
