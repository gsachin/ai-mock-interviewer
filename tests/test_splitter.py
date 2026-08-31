"""SentenceAccumulator: streaming sentence boundaries + hard caps."""
from interviewer.voice.splitter import SentenceAccumulator


def test_emits_complete_sentences_incrementally():
    acc = SentenceAccumulator()
    assert acc.feed("Hello") == []
    assert acc.feed(" there. ") == ["Hello there."]
    assert acc.feed("How are") == []
    assert acc.feed(" you? ") == ["How are you?"]
    assert acc.flush() == []


def test_flush_returns_trailing_partial():
    acc = SentenceAccumulator()
    acc.feed("One sentence. An unfinished")
    assert acc.flush() == ["An unfinished"]


def test_hard_cap_splits_long_sentences():
    acc = SentenceAccumulator(max_chars=20)
    long = "word " * 20          # 100 chars, no boundary
    parts = acc.feed(long)
    assert parts and all(len(p) <= 20 for p in parts)
    assert "".join(parts).startswith("word word")


def test_interviewer_style_deltas():
    acc = SentenceAccumulator()
    assert acc.feed("Design a rate limiter") == []
    # a sentence completes only once trailing whitespace arrives
    assert acc.feed(" for a public API.") == []
    assert acc.feed(" Expected points") == ["Design a rate limiter for a public API."]
    assert acc.flush() == ["Expected points"]
