"""v1 terminal-output R1-R8, E1-E2, example."""
import io
import re
import sys

import pytest

from nai import terminal
from nai.terminal import Streamer, log


class TTY(io.StringIO):
    def isatty(self):
        return True


def test_R1_example_exact(capsys):
    log("orch", "hello", "green")
    assert capsys.readouterr().out == "\x1b[32m[orch] hello\x1b[0m\n"


def test_R1_unknown_color():
    with pytest.raises(KeyError):
        log("x", "y", "purple")


def test_R2_transcript(tmp_path, capsys):
    p = tmp_path / "t.log"
    log("a", "no file yet")
    assert not p.exists()
    terminal.set_transcript(p)
    log("a", "line1\nline2", "red")
    line = p.read_text()
    assert re.fullmatch(r"\d\d:\d\d:\d\d \[a\] line1\nline2\n", line) and "\x1b" not in line   # E1


def test_R3_R4_thinking_then_text(capsys):
    s = Streamer("orch", True)
    s.think("ab")
    s.think("cd")
    s.text("hi")
    s.end()
    out = capsys.readouterr().out
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    assert plain == "[orch] thinking: abcd\n[orch] says: hi\n"


def test_R5_hidden_thinking_non_tty_prints_nothing(capsys):
    s = Streamer("orch", False)
    s.think("abc")
    s.tool("write_file", 5)
    s.end()
    assert capsys.readouterr().out == ""


def test_R5_R6_R7_tty_progress_throttled(monkeypatch):
    buf = TTY()
    monkeypatch.setattr(sys, "stdout", buf)
    t = [100.0]
    monkeypatch.setattr("nai.terminal.time.time", lambda: t[0])
    s = Streamer("coder", False)
    s.think("abc")            # first: printed immediately
    t[0] += 0.05
    s.think("defg")           # throttled
    t[0] += 0.3
    s.tool("", 12)            # printed
    out = buf.getvalue()
    assert "thinking... (3 chars)" in out and "(7 chars)" not in out
    assert "writing tool call ?: 12 chars" in out and out.count("\r\x1b[K") >= 2
    s.end()
    assert buf.getvalue().endswith("\r\x1b[K")          # R8: progress erased


def test_E2_end_with_nothing(capsys):
    Streamer("x", True).end()
    assert capsys.readouterr().out == ""
