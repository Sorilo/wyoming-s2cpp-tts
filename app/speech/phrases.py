"""Phase 9.5 Slice 1 — bounded deterministic PhraseAccumulator.

A pure, testable component that incrementally parses streaming text into
complete phrases using deterministic terminal-punctuation boundaries with
bounded fallback behavior.

Algorithm decisions:
- Defaults: soft=160, phrase=320, retained=640
- Terminal set: . ! ? 。 ！ ？
- Feed chunks verbatim; never insert spaces
- Decimal periods (digits on both sides) are protected
- Finite case-insensitive abbreviation set
- Ellipsis runs remain attached; no internal split
- A period is deferred when its boundary role can't be determined from
  the current buffer content:
  * Period at buffer end → deferred (may become ellipsis or abbreviation)
  * Period followed only by whitespace to buffer end → deferred (may be
    an abbreviation awaiting continuation)
  * Period followed by non-whitespace non-dot chars → confirmed boundary
    (unless protected by decimal/ellipsis/abbreviation rules)
- Non-period terminals (! ? 。 ！ ？) are always confirmed boundaries
- Fallback triggers when buffer exceeds phrase_max without a terminal.
  Order: last whitespace at/before soft threshold, then last whitespace
  at/before phrase_max, then hard split at phrase_max.
- Inter-phrase boundary whitespace is consumed after each emission
- Buffer enforcement: a single feed cannot leave retained > retained_max
- flush() emits non-whitespace residual exactly once
"""

from __future__ import annotations

_TERMINAL_CHARS: frozenset[str] = frozenset({".", "!", "?", "\u3002", "\uff01", "\uff1f"})

_ABBREVIATIONS: frozenset[str] = frozenset({
    "dr", "mr", "mrs", "ms", "prof",
    "sr", "jr", "st", "rev", "hon",
    "capt", "col", "gen", "lt", "maj",
    "sgt", "cpl", "gov", "sen", "rep",
    "esq",
})

_WHITESPACE: frozenset[str] = frozenset({" ", "\t", "\n", "\r"})

_CLOSERS: frozenset[str] = frozenset({'"', "'", "”", "’", ")", "]", "}"})


def _period_can_resolve(text: str, pos: int) -> bool:
    """A period at *pos* can be resolved when there's content after it
    that disambiguates its role.  Returns True if we can decide now,
    False if we must defer (chunking-invariance — more context may
    change the role of this period).
    """
    if pos >= len(text) or text[pos] != ".":
        return True  # not a period, no resolution needed

    # Scan past the period for significant content (skip whitespace and closers)
    i = pos + 1
    while i < len(text) and (text[i] in _WHITESPACE or text[i] in _CLOSERS):
        i += 1
    # If we hit EOF before finding a significant char, defer
    if i >= len(text):
        return False
    # A significant char exists — we can resolve
    return True


def _is_terminator(text: str, pos: int) -> bool:
    """Check if char at *pos* is a confirmed terminal boundary.

    For periods: must be resolvable AND not protected (decimal/ellipsis/abbrev).
    Other terminals: always confirmed.
    """
    ch = text[pos]
    if ch not in _TERMINAL_CHARS:
        return False

    if ch != ".":
        return True  # ! ? 。 ！ ？ are always confirmed

    # --- Period-specific checks ---

    # Must be resolvable (non-whitespace content after it)
    if not _period_can_resolve(text, pos):
        return False

    # Decimal: digit immediately on each side (no whitespace skip)
    if pos > 0 and text[pos - 1].isdigit():
        if pos + 1 < len(text) and text[pos + 1].isdigit():
            return False  # decimal — not a boundary

    # Ellipsis: adjacent dot
    if (pos > 0 and text[pos - 1] == ".") or (pos + 1 < len(text) and text[pos + 1] == "."):
        return False

    # Abbreviation: known word + whitespace + letter after the period
    start = pos - 1
    while start >= 0 and text[start].isalpha():
        start -= 1
    start += 1
    word = text[start:pos]
    if 1 <= len(word) <= 6 and word.lower() in _ABBREVIATIONS:
        i = pos + 1
        while i < len(text) and text[i] in _WHITESPACE:
            i += 1
        if i < len(text) and text[i].isalpha():
            return False  # abbreviation — not a boundary

    # All checks passed — confirmed sentence boundary
    return True


class PhraseAccumulator:
    """Bounded deterministic streaming phrase parser."""

    def __init__(
        self,
        soft_max: int = 160,
        phrase_max: int = 320,
        retained_max: int = 640,
    ) -> None:
        if phrase_max < soft_max:
            raise ValueError(
                f"phrase_max ({phrase_max}) must be >= soft_max ({soft_max})"
            )
        if retained_max < phrase_max:
            raise ValueError(
                f"retained_max ({retained_max}) must be >= phrase_max ({phrase_max})"
            )
        if soft_max < 1 or phrase_max < 1 or retained_max < 1:
            raise ValueError("limits must be positive")

        self.soft_max = soft_max
        self.phrase_max = phrase_max
        self.retained_max = retained_max
        self._buffer: str = ""
        self._flushed: bool = False
        self._emitted_any: bool = False

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buffer += chunk
        self._flushed = False  # new content arrived, allow future flush
        return self._extract_phrases()

    def _extract_phrases(self) -> list[str]:
        phrases: list[str] = []
        buf = self._buffer

        while True:
            buf_len = len(buf)
            if buf_len == 0:
                break

            # 1. Scan for confirmed terminal boundary
            boundary = None
            scan_end = min(buf_len, self.phrase_max)

            for i in range(scan_end):
                if _is_terminator(buf, i):
                    boundary = i + 1
                    break

            if boundary is not None:
                phrase = buf[:boundary]
                j = boundary
                # Consume closing quote/bracket chars (attach to phrase)
                while j < buf_len and buf[j] in _CLOSERS:
                    j += 1
                phrase = buf[:j]
                # Then consume inter-phrase whitespace
                while j < buf_len and buf[j] in _WHITESPACE:
                    j += 1
                buf = buf[j:]
                phrases.append(phrase)
                self._emitted_any = True
                continue

            # 2. No confirmed terminal. Fallback if buffer exceeds phrase_max.
            if buf_len > self.phrase_max:
                fallback = None

                limit = min(buf_len, self.soft_max)
                for i in range(limit - 1, -1, -1):
                    if buf[i] in _WHITESPACE:
                        fallback = i + 1
                        break

                if fallback is None:
                    limit = min(buf_len, self.phrase_max)
                    for i in range(limit - 1, -1, -1):
                        if buf[i] in _WHITESPACE:
                            fallback = i + 1
                            break

                if fallback is None:
                    fallback = min(buf_len, self.phrase_max)

                phrase = buf[:fallback]
                j = fallback
                while j < buf_len and buf[j] in _WHITESPACE:
                    j += 1
                buf = buf[j:]
                if phrase.strip():
                    phrases.append(phrase)
                    self._emitted_any = True
                continue

            # 3. Consume inter-phrase whitespace at buffer start
            if self._emitted_any and buf_len > 0 and buf[0] in _WHITESPACE:
                j = 0
                while j < buf_len and buf[j] in _WHITESPACE:
                    j += 1
                buf = buf[j:]
                continue

            # 4. Retain
            break

        # Enforce retained_max
        while len(buf) > self.retained_max:
            limit = min(len(buf), self.phrase_max)
            fallback = limit
            for i in range(limit - 1, -1, -1):
                if buf[i] in _WHITESPACE:
                    fallback = i + 1
                    break
            phrase = buf[:fallback]
            j = fallback
            while j < len(buf) and buf[j] in _WHITESPACE:
                j += 1
            buf = buf[j:]
            if phrase.strip():
                phrases.append(phrase)
                self._emitted_any = True

        self._buffer = buf
        return phrases

    def flush(self) -> str | None:
        if self._flushed:
            return None
        self._flushed = True

        stripped = self._buffer.strip()
        if not stripped:
            self._buffer = ""
            return None

        result = self._buffer
        self._buffer = ""
        return result
