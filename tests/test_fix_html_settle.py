import pytest

from cf_bypasser.core import challenge


class _Waiter:
    def doc_loaded(self, *a, **k):
        return True


class FakeTab:
    """tab.html returns a body whose size follows `sizes` (last value repeats)."""
    def __init__(self, sizes):
        self._sizes = list(sizes)
        self.calls = 0
        self.wait = _Waiter()

    @property
    def html(self):
        i = min(self.calls, len(self._sizes) - 1)
        self.calls += 1
        return "x" * self._sizes[i]


class GrowingTab:
    """Never stabilizes: every read is larger than the last."""
    def __init__(self):
        self.calls = 0
        self.wait = _Waiter()

    @property
    def html(self):
        self.calls += 1
        return "x" * (10 * self.calls)


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(challenge, "HTML_SETTLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(challenge, "HTML_SETTLE_STABLE_ROUNDS", 2)
    monkeypatch.setattr(challenge, "HTML_SETTLE_MAX_SECONDS", 5)


def test_returns_after_dom_stabilizes():
    assert len(challenge.stable_html(FakeTab([10, 20, 30, 30, 30]))) == 30


def test_stable_immediately():
    tab = FakeTab([42, 42, 42])
    assert len(challenge.stable_html(tab)) == 42
    assert tab.calls == 3  # initial read + 2 confirming rounds


def test_caps_at_max_and_does_not_hang(monkeypatch):
    monkeypatch.setattr(challenge, "HTML_SETTLE_MAX_SECONDS", 0.1)
    tab = GrowingTab()
    assert len(challenge.stable_html(tab)) > 0
    assert tab.calls < 100  # bounded by the deadline, never infinite


def test_disabled_when_rounds_zero(monkeypatch):
    monkeypatch.setattr(challenge, "HTML_SETTLE_STABLE_ROUNDS", 0)
    tab = FakeTab([10, 20, 30])
    assert len(challenge.stable_html(tab)) == 10  # first read, no polling
    assert tab.calls == 1
