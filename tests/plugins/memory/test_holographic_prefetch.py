"""Tests for HolographicMemoryProvider.prefetch raw-turn injection caps.

Covers:
- raw max (default 1)
- raw max age filter (default 30d)
- distilled unaffected
- config overrides respected

These test the public prefetch() seam per dispatch-loop T1 requirement.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from plugins.memory.holographic import HolographicMemoryProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 7, 26, 12, 0, 0)


def _make_result(kind="distilled", content="test fact", trust=0.5,
                 created_at: datetime | None = None) -> dict:
    """Build a single search result dict matching FactRetriever output shape."""
    return {
        "content": content,
        "kind": kind,
        "trust_score": trust,
        "trust": trust,
        "created_at": created_at if created_at is not None else NOW,
    }


def _setup_provider(raw_max=1, raw_max_age=30) -> HolographicMemoryProvider:
    """Create a provider with a mocked retriever returning controlled results."""
    provider = HolographicMemoryProvider(config={
        "prefetch_raw_max": raw_max,
        "prefetch_raw_max_age_days": raw_max_age,
        "min_trust_threshold": 0.3,
    })
    # Inject a mock retriever so we don't need a real DB
    provider._retriever = MagicMock()
    provider._min_trust = 0.3
    return provider


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPrefetchRawCap:
    """raw ≤ 1 條注入上限."""

    def test_single_raw_passes_through(self):
        """Only 1 raw turn → included."""
        p = _setup_provider()
        p._retriever.search.return_value = [
            _make_result("raw_turn", "user said hi", created_at=NOW),
        ]
        with patch("plugins.memory.holographic.HolographicMemoryProvider._format_age") as mock_fmt:
            mock_fmt.return_value = " · 今天"
            out = p.prefetch("Sue")
        assert "user said hi" in out
        assert "## 過往對話片段" in out

    def test_two_raw_capped_to_one(self):
        """2 raw turns → only one injected (the higher trust one)."""
        p = _setup_provider()
        p._retriever.search.return_value = [
            _make_result("raw_turn", "raw A", trust=0.5, created_at=NOW),
            _make_result("raw_turn", "raw B", trust=0.9, created_at=NOW),
        ]
        with patch("plugins.memory.holographic.HolographicMemoryProvider._format_age") as mock_fmt:
            mock_fmt.return_value = " · 今天"
            out = p.prefetch("Sue")
        assert "raw B" in out           # higher trust wins
        assert "raw A" not in out       # lower trust excluded

    def test_distilled_not_capped(self):
        """Distilled facts pass through unchanged (not limited by raw cap)."""
        p = _setup_provider(raw_max=1)
        p._retriever.search.return_value = [
            _make_result("distilled", "distilled A", trust=0.8),
            _make_result("distilled", "distilled B", trust=0.7),
        ]
        with patch("plugins.memory.holographic.HolographicMemoryProvider._format_age") as mock_fmt:
            mock_fmt.return_value = ""
            out = p.prefetch("work")
        assert "distilled A" in out
        assert "distilled B" in out


class TestPrefetchRawAgeFilter:
    """age > 30d raw_turn 被過濾."""

    def test_old_raw_excluded_at_31_days(self):
        p = _setup_provider(raw_max=5, raw_max_age=30)
        old_date = NOW - timedelta(days=32)
        p._retriever.search.return_value = [
            _make_result("raw_turn", "old raw", trust=0.9, created_at=old_date),
        ]
        with patch("plugins.memory.holographic.HolographicMemoryProvider._format_age") as mock_fmt:
            mock_fmt.return_value = " · 31天前"
            out = p.prefetch("anything")
        # Old raw excluded by age filter
        assert "## 過往對話片段" not in out

    def test_recent_raw_kept_at_10_days(self):
        p = _setup_provider(raw_max=5, raw_max_age=30)
        recent = NOW - timedelta(days=10)
        p._retriever.search.return_value = [
            _make_result("raw_turn", "recent raw", trust=0.9, created_at=recent),
        ]
        with patch("plugins.memory.holographic.HolographicMemoryProvider._format_age") as mock_fmt:
            mock_fmt.return_value = " · 10天前"
            out = p.prefetch("test")
        assert "recent raw" in out


class TestPrefetchConfig:
    """Config 開關可調."""

    def test_raw_max_custom(self):
        p = _setup_provider(raw_max=0)
        p._retriever.search.return_value = [
            _make_result("raw_turn", "should be excluded", trust=0.9),
        ]
        out = p.prefetch("nothing")
        # raw_max=0 → entire raw section omitted
        assert "## 過往對話片段" not in out

    def test_raw_max_age_custom_7(self):
        p = _setup_provider(raw_max=10, raw_max_age=7)
        within7 = NOW - timedelta(days=5)
        p._retriever.search.return_value = [
            _make_result("raw_turn", "inside window", trust=0.5, created_at=within7),
        ]
        with patch("plugins.memory.holographic.HolographicMemoryProvider._format_age") as mock_fmt:
            mock_fmt.return_value = " · 5天前"
            out = p.prefetch("query")
        assert "inside window" in out


def test_defaults_respected():
    """Default config: raw_max=1, raw_max_age=30."""
    p = HolographicMemoryProvider()
    assert p._prefetch_raw_max == 1
    assert p._prefetch_raw_max_age_days == 30