"""Phase 9.5 Slice 1 — exhaustive tests for bounded deterministic PhraseAccumulator.

Tests written BEFORE production implementation (strict TDD).
"""

import pytest
from app.speech.phrases import PhraseAccumulator

# ── Terminal punctuation and basic boundaries ──────────────────────────


def test_single_terminal_period():
    """Single sentence ending in . — period at buffer end is deferred until flush."""
    acc = PhraseAccumulator()
    phrases = acc.feed("Hello world.")
    assert phrases == []
    assert acc.flush() == "Hello world."


def test_single_terminal_exclamation():
    """Single sentence ending in !"""
    acc = PhraseAccumulator()
    phrases = acc.feed("Hello world!")
    assert phrases == ["Hello world!"]


def test_single_terminal_question():
    """Single sentence ending in ?"""
    acc = PhraseAccumulator()
    phrases = acc.feed("Hello world?")
    assert phrases == ["Hello world?"]


def test_cjk_full_stop():
    """Chinese full stop"""
    acc = PhraseAccumulator()
    phrases = acc.feed("\u4f60\u597d\u4e16\u754c\u3002")
    assert phrases == ["\u4f60\u597d\u4e16\u754c\u3002"]


def test_cjk_exclamation_fullwidth():
    """Chinese fullwidth exclamation"""
    acc = PhraseAccumulator()
    phrases = acc.feed("\u4f60\u597d\u4e16\u754c\uff01")
    assert phrases == ["\u4f60\u597d\u4e16\u754c\uff01"]


def test_cjk_question_fullwidth():
    """Chinese fullwidth question"""
    acc = PhraseAccumulator()
    phrases = acc.feed("\u4f60\u597d\u4e16\u754c\uff1f")
    assert phrases == ["\u4f60\u597d\u4e16\u754c\uff1f"]


def test_multiple_phrases_single_chunk():
    acc = PhraseAccumulator()
    phrases = acc.feed("First. Second! Third?")
    assert phrases == ["First.", "Second!", "Third?"]


def test_no_terminal_no_emit():
    acc = PhraseAccumulator()
    phrases = acc.feed("Hello world")
    assert phrases == []


def test_flush_residual():
    acc = PhraseAccumulator()
    acc.feed("Hello world")
    residual = acc.flush()
    assert residual == "Hello world"


def test_flush_empty():
    acc = PhraseAccumulator()
    residual = acc.flush()
    assert residual is None


def test_flush_whitespace_only():
    acc = PhraseAccumulator()
    acc.feed("   \n\t  ")
    residual = acc.flush()
    assert residual is None


def test_flush_exactly_once():
    acc = PhraseAccumulator()
    acc.feed("Hello")
    first = acc.flush()
    assert first == "Hello"
    second = acc.flush()
    assert second is None


def test_feed_after_flush():
    acc = PhraseAccumulator()
    acc.feed("Hello")
    acc.flush()
    phrases = acc.feed("Again.")
    assert phrases == []
    assert acc.flush() == "Again."


def test_phrase_split_across_chunks():
    acc = PhraseAccumulator()
    p1 = acc.feed("Hello wor")
    assert p1 == []
    p2 = acc.feed("ld.")
    assert p2 == []  # period at buffer end, deferred
    assert acc.flush() == "Hello world."


def test_punctuation_in_next_chunk():
    acc = PhraseAccumulator()
    p1 = acc.feed("Hello world")
    assert p1 == []
    p2 = acc.feed(".")
    assert p2 == []  # bare period at buffer end, deferred
    assert acc.flush() == "Hello world."


def test_multiple_phrases_across_chunks():
    acc = PhraseAccumulator()
    p1 = acc.feed("First.")
    assert p1 == []  # period at end, deferred
    p2 = acc.feed("Second.")
    assert p2 == ["First."]  # "First." confirmed when "Second." arrives
    assert acc.flush() == "Second."


def test_multiple_punctuation_across_chunks():
    acc = PhraseAccumulator()
    p1 = acc.feed("First. Second. Third")
    assert p1 == ["First.", "Second."]  # "First." and "Second." confirmed (followed by text)
    p2 = acc.feed(".")
    assert p2 == []  # "Third." — period at end deferred
    assert acc.flush() == "Third."


# ── Whitespace handling ────────────────────────────────────────────────


def test_leading_whitespace_preserved():
    acc = PhraseAccumulator()
    phrases = acc.feed("  Hello world.")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "  Hello world."


def test_trailing_whitespace_in_phrase():
    acc = PhraseAccumulator()
    phrases = acc.feed("Hello world   .")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "Hello world   ."


def test_inter_phrase_whitespace_consumed():
    acc = PhraseAccumulator()
    phrases = acc.feed("First.  \n  Second.")
    assert phrases == ["First."]  # "Second." period at end, deferred
    assert acc.flush() == "Second."


def test_inter_phrase_whitespace_across_chunks():
    acc = PhraseAccumulator()
    p1 = acc.feed("First. ")
    assert p1 == []  # period followed only by whitespace to buffer end, deferred
    p2 = acc.feed(" \n  Second.")
    assert p2 == ["First."]  # "First." now confirmed; "Second." period at end deferred
    assert acc.flush() == "Second."


# ── Chunking invariance ────────────────────────────────────────────────


def test_chunking_invariance_normal():
    text = "Hello world. This is a test. Goodbye."

    def collect(chunks):
        acc = PhraseAccumulator()
        result = []
        for c in chunks:
            result.extend(acc.feed(c))
        residual = acc.flush()
        if residual:
            result.append(residual)
        return result

    full = collect([text])
    split1 = collect(["Hello world. This is a test. Goodbye."])
    split2 = collect(["Hello world. ", "This is a test. Goodbye."])
    split3 = collect(["Hello world. This", " is a test. Goodbye."])
    split4 = collect(["Hello", " world. This is", " a test. Goodbye."])
    split5 = collect(list(text))

    assert full == ["Hello world.", "This is a test.", "Goodbye."]
    assert split1 == full
    assert split2 == full
    assert split3 == full
    assert split4 == full
    assert split5 == full


# ── Decimal protection ─────────────────────────────────────────────────


def test_decimal_period_protected():
    acc = PhraseAccumulator()
    phrases = acc.feed("The price is 3.14 dollars.")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "The price is 3.14 dollars."


def test_decimal_at_eof():
    acc = PhraseAccumulator()
    phrases = acc.feed("The price is 3.14")
    assert phrases == []
    residual = acc.flush()
    assert residual == "The price is 3.14"


def test_decimal_across_chunks():
    acc = PhraseAccumulator()
    p1 = acc.feed("The price is 3")
    assert p1 == []
    p2 = acc.feed(".14 dollars.")
    assert p2 == []  # period at end, deferred
    assert acc.flush() == "The price is 3.14 dollars."


def test_decimal_multiple_in_sentence():
    acc = PhraseAccumulator()
    phrases = acc.feed("Values 1.5 and 2.75 are good.")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "Values 1.5 and 2.75 are good."


def test_decimal_chunking_invariance():
    text = "Pi is 3.14159. Approx 2.718."
    for chunks in [
        [text],
        ["Pi is 3.14159. Approx 2.718."],
        ["Pi is 3.14", "159. Approx 2.718."],
        ["Pi is ", "3.14159. Approx ", "2.718."],
    ]:
        acc = PhraseAccumulator()
        result = []
        for c in chunks:
            result.extend(acc.feed(c))
        res = acc.flush()
        if res:
            result.append(res)
        assert result == ["Pi is 3.14159.", "Approx 2.718."], f"Failed for chunks {chunks}"


# ── Abbreviation protection ───────────────────────────────────────────


def test_abbreviation_no_split():
    acc = PhraseAccumulator()
    phrases = acc.feed("Dr. Smith is here.")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "Dr. Smith is here."


def test_abbreviation_mr():
    acc = PhraseAccumulator()
    phrases = acc.feed("Mr. Jones arrived.")
    assert phrases == []
    assert acc.flush() == "Mr. Jones arrived."


def test_abbreviation_mrs():
    acc = PhraseAccumulator()
    phrases = acc.feed("Mrs. Jones arrived.")
    assert phrases == []
    assert acc.flush() == "Mrs. Jones arrived."


def test_abbreviation_ms():
    acc = PhraseAccumulator()
    phrases = acc.feed("Ms. Jones arrived.")
    assert phrases == []
    assert acc.flush() == "Ms. Jones arrived."


def test_abbreviation_prof():
    acc = PhraseAccumulator()
    phrases = acc.feed("Prof. Einstein taught.")
    assert phrases == []
    assert acc.flush() == "Prof. Einstein taught."


def test_abbreviation_case_insensitive():
    acc = PhraseAccumulator()
    phrases = acc.feed("DR. Smith is here.")
    assert phrases == []
    assert acc.flush() == "DR. Smith is here."


def test_abbreviation_across_chunks():
    acc = PhraseAccumulator()
    p1 = acc.feed("Dr")
    assert p1 == []
    p2 = acc.feed(". Smith is here.")
    assert p2 == []  # period at end, deferred
    assert acc.flush() == "Dr. Smith is here."


def test_abbreviation_plus_sentence():
    acc = PhraseAccumulator()
    phrases = acc.feed("Dr. Smith arrived. He is early.")
    assert phrases == ["Dr. Smith arrived."]  # "He is early." — period at end, deferred
    assert acc.flush() == "He is early."


def test_abbreviation_multipart():
    acc = PhraseAccumulator()
    phrases = acc.feed("Dr. Mr. Smith is present.")
    assert phrases == []
    assert acc.flush() == "Dr. Mr. Smith is present."


def test_not_abbreviation_single_letter():
    acc = PhraseAccumulator()
    phrases = acc.feed("A. This is a test.")
    assert phrases == ["A."]  # "A." confirmed (followed by space+text); "test." deferred
    assert acc.flush() == "This is a test."


# ── Ellipsis protection ────────────────────────────────────────────────


def test_ellipsis_no_internal_split():
    acc = PhraseAccumulator()
    phrases = acc.feed("Hello... world.")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "Hello... world."


def test_ellipsis_triple_dot():
    acc = PhraseAccumulator()
    phrases = acc.feed("Wait... something happened.")
    assert phrases == []
    assert acc.flush() == "Wait... something happened."


def test_ellipsis_four_dots():
    acc = PhraseAccumulator()
    phrases = acc.feed("Wait.... something happened.")
    assert phrases == []
    assert acc.flush() == "Wait.... something happened."


def test_ellipsis_across_chunks():
    acc = PhraseAccumulator()
    p1 = acc.feed("Wait.")
    assert p1 == []  # period at end, deferred
    p2 = acc.feed(".. something happened.")
    assert p2 == []  # period at end, deferred
    assert acc.flush() == "Wait... something happened."


# ── Bounded fallback ───────────────────────────────────────────────────


def test_soft_fallback_at_whitespace():
    acc = PhraseAccumulator(soft_max=30, phrase_max=80, retained_max=200)
    phrases = acc.feed("these are some words without end")
    assert phrases == []


def test_soft_fallback_emits():
    acc = PhraseAccumulator(soft_max=20, phrase_max=40, retained_max=100)
    phrases = acc.feed("this is a longer string without any punctuation marks at all")
    assert len(phrases) >= 1


def test_hard_max_fallback():
    acc = PhraseAccumulator(soft_max=10, phrase_max=20, retained_max=100)
    long_word = "a" * 50
    phrases = acc.feed(long_word)
    assert len(phrases) >= 1
    assert len(phrases[0]) <= 20


def test_buffer_enforcement():
    acc = PhraseAccumulator(soft_max=20, phrase_max=40, retained_max=50)
    long_text = "a" * 100
    phrases = acc.feed(long_text)
    assert len(phrases) > 1


def test_overlong_single_token():
    acc = PhraseAccumulator(soft_max=5, phrase_max=10, retained_max=20)
    token = "X" * 55
    phrases = acc.feed(token)
    for p in phrases:
        assert len(p) <= 10
    total_emitted = sum(len(p) for p in phrases)
    assert total_emitted >= 50


# ── Mixed CJK/ASCII ────────────────────────────────────────────────────


def test_mixed_cjk_ascii():
    acc = PhraseAccumulator()
    phrases = acc.feed("Hello\u4e16\u754c\u3002This is a test.")
    assert phrases == ["Hello\u4e16\u754c\u3002"]  # CJK stop not deferred; period at end deferred
    assert acc.flush() == "This is a test."


def test_mixed_cjk_ascii_across_chunks():
    acc = PhraseAccumulator()
    p1 = acc.feed("Hello\u4e16\u754c\u3002This")
    assert p1 == ["Hello\u4e16\u754c\u3002"]  # CJK stop not deferred
    p2 = acc.feed(" is a test.")
    assert p2 == []  # period at end, deferred
    assert acc.flush() == "This is a test."


# ── Degenerate/edge cases ──────────────────────────────────────────────


def test_empty_chunk():
    acc = PhraseAccumulator()
    phrases = acc.feed("")
    assert phrases == []


def test_multiple_empty_chunks():
    acc = PhraseAccumulator()
    for _ in range(5):
        assert acc.feed("") == []
    assert acc.flush() is None


def test_bare_punctuation():
    acc = PhraseAccumulator()
    phrases = acc.feed(".")
    assert phrases == []  # bare period at end, deferred
    assert acc.flush() == "."


def test_bare_exclamation():
    acc = PhraseAccumulator()
    phrases = acc.feed("!")
    assert phrases == ["!"]


def test_bare_cjk_punctuation():
    acc = PhraseAccumulator()
    phrases = acc.feed("\u3002")
    assert phrases == ["\u3002"]


def test_punctuation_only_sequence():
    acc = PhraseAccumulator()
    phrases = acc.feed(".!?")
    assert phrases == [".", "!", "?"]


def test_only_whitespace_and_punctuation():
    acc = PhraseAccumulator()
    phrases = acc.feed("   .   !")
    assert phrases == ["   .", "!"]


def test_newline_as_whitespace():
    acc = PhraseAccumulator()
    phrases = acc.feed("Line one.\nLine two.")
    assert phrases == ["Line one."]  # "Line one." confirmed (followed by \n+text)
    assert acc.flush() == "Line two."


def test_tab_as_whitespace():
    acc = PhraseAccumulator()
    phrases = acc.feed("First.\tSecond.")
    assert phrases == ["First."]  # "First." confirmed (followed by \t+text)
    assert acc.flush() == "Second."


def test_multiple_terminals_together():
    acc = PhraseAccumulator()
    phrases = acc.feed("What?! Really.")
    assert phrases == ["What?", "!"]  # "Really." period at end, deferred
    assert acc.flush() == "Really."


def test_whitespace_before_terminal():
    acc = PhraseAccumulator()
    phrases = acc.feed("Hello world  .")
    assert phrases == []
    assert acc.flush() == "Hello world  ."


# ── Chunking invariance (all cases) ────────────────────────────────────


def _collect(text, chunks=None):
    acc = PhraseAccumulator()
    result = []
    if chunks is None:
        result.extend(acc.feed(text))
    else:
        for c in chunks:
            result.extend(acc.feed(c))
    residual = acc.flush()
    if residual is not None:
        result.append(residual)
    return result


def _check_invariant(text, label):
    reference = _collect(text)
    for split_at in range(1, len(text)):
        chunks = [text[:split_at], text[split_at:]]
        result = _collect(text, chunks)
        assert result == reference, f"{label} split at {split_at}: {result} != {reference}"


def test_chunking_invariance_cjk():
    _check_invariant("\u4f60\u597d\u4e16\u754c\u3002\u8fd9\u662f\u6d4b\u8bd5\u3002\u518d\u89c1\u3002", "CJK")


def test_chunking_invariance_decimal():
    _check_invariant("Pi is 3.14159. E is 2.718.", "decimal")


def test_chunking_invariance_abbreviation():
    _check_invariant("Dr. Smith arrived. Prof. Jones left.", "abbreviation")


def test_chunking_invariance_ellipsis():
    _check_invariant("Wait... something happened. Really.", "ellipsis")


def test_chunking_invariance_mixed_cjk():
    _check_invariant("Hello\u4e16\u754c\u3002This is a test. Goodbye\u3002", "mixed_cjk")


def test_chunking_invariance_edge_punctuation():
    _check_invariant("Hello. . Test. What?! Really.", "edge_punctuation")


# ── Configuration validation ───────────────────────────────────────────


def test_default_limits():
    acc = PhraseAccumulator()
    assert acc.soft_max == 160
    assert acc.phrase_max == 320
    assert acc.retained_max == 640


def test_custom_limits():
    acc = PhraseAccumulator(soft_max=50, phrase_max=100, retained_max=200)
    assert acc.soft_max == 50
    assert acc.phrase_max == 100
    assert acc.retained_max == 200


def test_phrase_max_must_be_greater_than_soft_max():
    with pytest.raises(ValueError):
        PhraseAccumulator(soft_max=100, phrase_max=50)


def test_retained_max_must_be_greater_than_phrase_max():
    with pytest.raises(ValueError):
        PhraseAccumulator(soft_max=50, phrase_max=100, retained_max=80)


# ── Closing quote/bracket attachment ────────────────────────────────────


def test_closing_straight_double_quote_attached():
    """Terminal punctuation followed immediately by closing " must stay attached."""
    acc = PhraseAccumulator()
    phrases = acc.feed('"Hello."')
    assert phrases == []  # period at buffer end, deferred
    assert acc.flush() == '"Hello."'


def test_closing_straight_single_quote_attached():
    """Terminal punctuation followed immediately by closing ' must stay attached."""
    acc = PhraseAccumulator()
    phrases = acc.feed("'Hello.'")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "'Hello.'"


def test_closing_curly_double_quote_attached():
    """Terminal punctuation followed immediately by closing \u201d must stay attached."""
    acc = PhraseAccumulator()
    text = '\u201cHello.\u201d'
    phrases = acc.feed(text)
    assert phrases == []  # period at end, deferred
    assert acc.flush() == text


def test_closing_curly_single_quote_attached():
    """Terminal punctuation followed immediately by closing \u2019 must stay attached."""
    acc = PhraseAccumulator()
    text = '\u2018Hello.\u2019'
    phrases = acc.feed(text)
    assert phrases == []  # period at end, deferred
    assert acc.flush() == text


def test_closing_paren_attached():
    """Terminal punctuation followed by ) must stay attached."""
    acc = PhraseAccumulator()
    phrases = acc.feed("(Hello.)")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "(Hello.)"


def test_closing_bracket_attached():
    """Terminal punctuation followed by ] must stay attached."""
    acc = PhraseAccumulator()
    phrases = acc.feed("[Hello.]")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "[Hello.]"


def test_closing_brace_attached():
    """Terminal punctuation followed by } must stay attached."""
    acc = PhraseAccumulator()
    phrases = acc.feed("{Hello.}")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "{Hello.}"


def test_closing_quote_not_orphaned_mid_text():
    """Closing quote after terminal should attach, not be orphaned between phrases."""
    acc = PhraseAccumulator()
    phrases = acc.feed('"Hello." More text.')
    assert phrases == ['"Hello."']
    assert acc.flush() == "More text."


def test_closing_run_multiple():
    """Multiple closers after terminal punctuation should all attach."""
    acc = PhraseAccumulator()
    phrases = acc.feed('"Hello.")')
    assert phrases == []
    assert acc.flush() == '"Hello.")'


def test_closing_quote_chunk_split():
    """Closing quote arriving in a later chunk must still attach."""
    acc = PhraseAccumulator()
    p1 = acc.feed('"Hello.')
    assert p1 == []
    p2 = acc.feed('"')
    assert p2 == []
    assert acc.flush() == '"Hello."'


def test_closing_quote_exclamation():
    """Closing quote after ! must stay attached."""
    acc = PhraseAccumulator()
    phrases = acc.feed('"Hello!"')
    assert phrases == ['"Hello!"']


def test_closing_quote_question():
    """Closing quote after ? must stay attached."""
    acc = PhraseAccumulator()
    phrases = acc.feed('"Hello?"')
    assert phrases == ['"Hello?"']


def test_closing_quote_cjk():
    """Closing quote after CJK full stop must stay attached."""
    acc = PhraseAccumulator()
    text = '\u201c\u4f60\u597d\u3002\u201d'
    phrases = acc.feed(text)
    assert phrases == [text]


# ── Decimal boundary (strict immediate digits only) ────────────────────


def test_decimal_no_space_protected():
    """3.14 without space is protected as decimal."""
    acc = PhraseAccumulator()
    phrases = acc.feed("The value is 3.14.")
    assert phrases == []  # period at end, deferred
    assert acc.flush() == "The value is 3.14."


def test_decimal_with_space_splits():
    """3. 14 with a space after period must split — digit must be immediate."""
    acc = PhraseAccumulator()
    phrases = acc.feed("Number 3. 14 more.")
    assert phrases == ["Number 3."]
    assert acc.flush() == "14 more."


def test_decimal_with_space_chunk_split():
    """Chunk-split across the spaced decimal boundary must still split."""
    acc = PhraseAccumulator()
    p1 = acc.feed("Number 3.")
    assert p1 == []  # period at end, deferred
    p2 = acc.feed(" 14 more.")
    assert p2 == ["Number 3."]
    assert acc.flush() == "14 more."


def test_decimal_immediate_digit_eof():
    """3.14 at EOF without trailing period should not split."""
    acc = PhraseAccumulator()
    phrases = acc.feed("The value is 3.14")
    assert phrases == []
    assert acc.flush() == "The value is 3.14"


def test_decimal_immediate_digit_plus_sentence():
    """3.14 followed by a real sentence boundary should protect the decimal."""
    acc = PhraseAccumulator()
    phrases = acc.feed("Pi is 3.14. That is correct.")
    assert phrases == ["Pi is 3.14."]
    assert acc.flush() == "That is correct."




# ── Deterministic reproducibility ──────────────────────────────────────


def test_deterministic_output():
    text = "Dr. Smith arrived at 3.14 PM. Hello... world! How are you? I am fine."
    reference = _collect(text)
    for _ in range(10):
        assert _collect(text) == reference
