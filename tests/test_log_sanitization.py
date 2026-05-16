"""Tests for SEC-106: log-sanitization in `_one_line`.

`progress.log` is appended line-by-line and is intended to be read
via `tail -f`. A malicious task string or LLM-controlled `result`
must not be able to smuggle terminal escape sequences (ANSI / CSI),
embedded NULs, or any other control char into the viewer's terminal.
"""
from __future__ import annotations

from agent_callstack.frames import _one_line


def test_strips_ansi_escape_sequences():
    # CSI escape ESC=\x1b followed by '[2J' clears the terminal.
    src = "\x1b[2J\x1b[1;31mevil\x1b[0m"
    out = _one_line(src, 60)
    assert "\x1b" not in out, "ANSI escape leaked through _one_line"
    # The visible text survives, just with the control bytes replaced.
    assert "evil" in out


def test_strips_null_and_bell():
    src = "before\x00middle\x07after"
    out = _one_line(src, 60)
    assert "\x00" not in out
    assert "\x07" not in out
    assert "before" in out
    assert "after" in out


def test_collapses_newlines_and_tabs_to_space():
    src = "line1\nline2\tcol2\r\nend"
    out = _one_line(src, 60)
    assert "\n" not in out and "\r" not in out and "\t" not in out
    assert "line1 line2 col2  end" == out or "line1 line2 col2 end" in out


def test_preserves_unicode():
    src = "héllo → world 🌍"
    out = _one_line(src, 60)
    assert "héllo" in out
    assert "→" in out
    assert "🌍" in out


def test_replaces_double_quote_with_apostrophe():
    src = 'he said "hi"'
    assert _one_line(src, 60) == "he said 'hi'"


def test_strips_delete_char():
    # 0x7F (DEL) can also disrupt terminals.
    src = "before\x7Fafter"
    assert "\x7F" not in _one_line(src, 60)


def test_truncation_still_works_after_sanitization():
    # 30-char string with embedded ANSI should still truncate to limit.
    src = "\x1b[31m" + "x" * 50 + "\x1b[0m"
    out = _one_line(src, 20)
    assert len(out) == 20
    assert out.endswith("…")
    assert "\x1b" not in out
