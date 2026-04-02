"""Tests for easter_eggs.py — flavor text rotation, uptime milestones."""

import time
import pytest
from sensewatch import easter_eggs


class TestPick:
    def test_returns_string_from_pool(self):
        pool = ["a", "b", "c", "d", "e", "f"]
        result = easter_eggs.pick(pool)
        assert result in pool

    def test_avoids_recent_picks(self):
        pool = ["a", "b", "c", "d", "e", "f", "g", "h"]
        # Reset history for this pool
        easter_eggs._recent_picks.pop(id(pool), None)

        seen = set()
        for _ in range(5):
            result = easter_eggs.pick(pool)
            seen.add(result)

        # All 5 picks should be unique (no repeats in last 5)
        assert len(seen) == 5

    def test_cycles_through_all(self):
        pool = ["x", "y", "z"]
        easter_eggs._recent_picks.pop(id(pool), None)

        seen = set()
        for _ in range(10):
            seen.add(easter_eggs.pick(pool))

        # Should eventually use all items
        assert seen == {"x", "y", "z"}


class TestFlavorText:
    def test_idle_flavor_has_content(self):
        text = easter_eggs.flavor_text(running_jobs=3, gpu_total=312)
        assert len(text) > 0

    def test_disconnected_flavor(self):
        text = easter_eggs.flavor_text(connected=False)
        assert text in easter_eggs.FLAVOR_DISCONNECTED

    def test_format_variables_substituted(self):
        # Force a specific message with format variables
        easter_eggs._recent_picks.pop(id(easter_eggs.FLAVOR_IDLE), None)
        # Run enough times that we'll hit one with format vars
        for _ in range(100):
            text = easter_eggs.flavor_text(
                running_jobs=5, gpu_total=100, start_time="14:30"
            )
            # Should never have unresolved {brackets}
            assert "{" not in text


class TestNotifySubtitle:
    def test_known_types(self):
        for t in ["running", "succeeded", "failed", "stopped",
                   "connection_lost", "connection_restored"]:
            result = easter_eggs.notify_subtitle(t)
            assert isinstance(result, str)
            assert len(result) > 0

    def test_unknown_type_falls_back(self):
        result = easter_eggs.notify_subtitle("unknown_thing")
        assert isinstance(result, str)


class TestGPUCommentary:
    def test_full(self):
        result = easter_eggs.gpu_commentary(0, 312)
        assert result in easter_eggs.GPU_FULL

    def test_scarce(self):
        result = easter_eggs.gpu_commentary(30, 312)  # ~10%
        assert result in easter_eggs.GPU_SCARCE

    def test_moderate(self):
        result = easter_eggs.gpu_commentary(100, 312)  # ~32%
        assert result in easter_eggs.GPU_MODERATE

    def test_plenty(self):
        result = easter_eggs.gpu_commentary(200, 312)  # ~64%
        assert result in easter_eggs.GPU_PLENTY

    def test_zero_total(self):
        assert easter_eggs.gpu_commentary(0, 0) == ""


class TestUptimeCheck:
    def test_no_milestone_early(self):
        easter_eggs._uptime_notified.clear()
        result = easter_eggs.uptime_check(time.time() - 60)  # 1 minute
        assert result is None

    def test_one_hour_milestone(self):
        easter_eggs._uptime_notified.clear()
        result = easter_eggs.uptime_check(time.time() - 3700)  # just over 1hr
        assert result is not None
        assert "1 hour" in result

    def test_milestone_only_fires_once(self):
        easter_eggs._uptime_notified.clear()
        start = time.time() - 3700
        first = easter_eggs.uptime_check(start)
        second = easter_eggs.uptime_check(start)
        assert first is not None
        assert second is None
