"""terminal-output: colored log lines, transcript file, live stream rendering (v1 terminal-output)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

COLORS = dict(dim="\033[2m", cyan="\033[36m", yellow="\033[33m", green="\033[32m", red="\033[31m", end="\033[0m")
_TRANSCRIPT: Path | None = None


def set_transcript(path: Path | None) -> None:
    global _TRANSCRIPT
    _TRANSCRIPT = Path(path) if path else None


def log(tag: str, text: str, color: str = "dim") -> None:
    """R1/R2: print `[tag] text` in color (KeyError for unknown colors) and append to the transcript if set."""
    line = f"[{tag}] {text}"
    print(f"{COLORS[color]}{line}{COLORS['end']}", flush=True)
    if _TRANSCRIPT:
        with open(_TRANSCRIPT, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")


class Streamer:
    """Live rendering of a streaming completion: thinking (dim), text (green), tool-call progress."""

    def __init__(self, tag: str, show_thinking: bool):
        self.tag, self.show_thinking = tag, show_thinking
        self.mode: str | None = None
        self.last_progress = 0.0
        self.think_chars = 0
        self.tty = sys.stdout.isatty()

    def _w(self, text: str, color: str):
        sys.stdout.write(f"{COLORS[color]}{text}{COLORS['end']}")
        sys.stdout.flush()

    def _clear_progress(self):
        if self.tty and self.last_progress:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        self.last_progress = 0.0

    def _begin(self, mode: str, label: str, color: str):
        self._clear_progress()
        if self.mode is not None:
            sys.stdout.write("\n")
        self.mode = mode
        self._w(f"[{self.tag}] {label}: ", color)

    def _progress(self, msg: str):
        if not self.tty:
            return
        now = time.time()
        if self.last_progress and now - self.last_progress < 0.2:
            return
        if self.mode is not None:
            sys.stdout.write("\n")
            self.mode = None
        self.last_progress = now
        self._w(f"\r\033[K[{self.tag}] {msg}", "dim")

    def think(self, s: str):
        self.think_chars += len(s)
        if self.show_thinking:
            if self.mode != "think":
                self._begin("think", "thinking", "dim")
            self._w(s, "dim")
        else:
            self._progress(f"thinking... ({self.think_chars} chars)")

    def text(self, s: str):
        if self.mode != "text":
            self._begin("text", "says", "green")
        self._w(s, "green")

    def tool(self, name: str, nchars: int):
        self._progress(f"writing tool call {name or '?'}: {nchars} chars")

    def end(self):
        self._clear_progress()
        if self.mode is not None:
            sys.stdout.write("\n")
        sys.stdout.flush()
        self.mode = None
