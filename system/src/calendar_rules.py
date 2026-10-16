"""
Working-day arithmetic and the cut-off rule.

Two rules from how the process actually runs:

1. TAT is counted in working days, on a shared holiday calendar, with a
   per-acquirer override for banks whose holidays differ.
2. A request raised after the 2pm cut-off starts the acquirer's clock on the
   next working day. This is why batches go out first thing in the morning.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

CUTOFF = time(14, 0)

WEEKEND = {5, 6}  # Saturday, Sunday


class WorkingCalendar:
    """Named calendars, so one acquirer can differ from the default without
    forking the whole engine. Holidays are editable config, not code."""

    def __init__(self, holidays: dict[str, set[date]] | None = None) -> None:
        self._holidays = holidays or {"default": set()}
        if "default" not in self._holidays:
            self._holidays["default"] = set()

    def _days_off(self, calendar: str) -> set[date]:
        return self._holidays.get(calendar, self._holidays["default"])

    def is_working_day(self, d: date, calendar: str = "default") -> bool:
        return d.weekday() not in WEEKEND and d not in self._days_off(calendar)

    def next_working_day(self, d: date, calendar: str = "default") -> date:
        """The first working day strictly after d."""
        nxt = d + timedelta(days=1)
        while not self.is_working_day(nxt, calendar):
            nxt += timedelta(days=1)
        return nxt

    def effective_start(self, at: datetime, calendar: str = "default") -> date:
        """The day a clock actually starts, given the cut-off.

        Same day if it is a working day and the time is at or before 2pm.
        Otherwise the next working day. A request logged on a Saturday starts
        on Monday regardless of the hour.
        """
        d = at.date()
        if self.is_working_day(d, calendar) and at.time() <= CUTOFF:
            return d
        return self.next_working_day(d, calendar)

    def working_days_between(
        self, start: date, end: date, calendar: str = "default"
    ) -> int:
        """Working days from start to end, counting the start day and not the
        end day.

        Start and end on the same working day is 1 day, not 0: a request
        handled and passed on within a single day still consumed a day of that
        party's time, and reporting it as zero would understate held time on
        every fast handoff.
        """
        if end < start:
            raise ValueError(f"end {end} precedes start {start}")

        if start == end:
            return 1 if self.is_working_day(start, calendar) else 0

        count = 0
        cursor = start
        while cursor < end:
            if self.is_working_day(cursor, calendar):
                count += 1
            cursor += timedelta(days=1)
        return count


def load_calendar(config: dict[str, list[str]]) -> WorkingCalendar:
    """Build a calendar from config of {calendar_name: ["YYYY-MM-DD", ...]}."""
    holidays = {
        name: {date.fromisoformat(d) for d in days} for name, days in config.items()
    }
    return WorkingCalendar(holidays)
