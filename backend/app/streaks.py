from __future__ import annotations

from datetime import date, timedelta


def compute_streaks(log_dates: list[date], today: date | None = None) -> tuple[int, int]:
    """Pure function: given the distinct dates a user logged something for a
    book, return (current_streak_days, longest_streak_days).

    current_streak_days is 0 if the most recent logged date isn't today or
    yesterday (a broken streak shouldn't display as "current").
    """
    if not log_dates:
        return 0, 0
    today = today or date.today()
    dates = sorted(set(log_dates))

    runs: list[int] = []
    run_length = 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            run_length += 1
        else:
            runs.append(run_length)
            run_length = 1
    runs.append(run_length)

    longest = max(runs)
    current = runs[-1] if dates[-1] in (today, today - timedelta(days=1)) else 0
    return current, longest
