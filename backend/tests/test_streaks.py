from datetime import date, timedelta

from app.streaks import compute_streaks


def test_compute_streaks_empty_returns_zero() -> None:
    assert compute_streaks([]) == (0, 0)


def test_compute_streaks_single_day_today_is_current_streak_one() -> None:
    today = date(2026, 7, 31)
    assert compute_streaks([today], today=today) == (1, 1)


def test_compute_streaks_consecutive_days_ending_today() -> None:
    today = date(2026, 7, 31)
    dates = [today - timedelta(days=i) for i in range(5)]
    assert compute_streaks(dates, today=today) == (5, 5)


def test_compute_streaks_broken_streak_not_ending_today_is_zero_current() -> None:
    today = date(2026, 7, 31)
    dates = [today - timedelta(days=5), today - timedelta(days=6)]
    current, longest = compute_streaks(dates, today=today)
    assert current == 0
    assert longest == 2


def test_compute_streaks_longest_beats_current() -> None:
    today = date(2026, 7, 31)
    # a 4-day run last week, then a 1-day run today
    dates = [today] + [today - timedelta(days=10 + i) for i in range(4)]
    current, longest = compute_streaks(dates, today=today)
    assert current == 1
    assert longest == 4


def test_compute_streaks_yesterday_still_counts_as_current() -> None:
    today = date(2026, 7, 31)
    dates = [today - timedelta(days=1), today - timedelta(days=2)]
    current, _ = compute_streaks(dates, today=today)
    assert current == 2
