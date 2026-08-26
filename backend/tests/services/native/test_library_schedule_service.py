"""Interval schedule due instants across DST transitions (F-001 regression net).

The rolling-gap contract (`api/v1/schemas/settings.py`: "the interval values run
on a rolling gap since the last scan") is elapsed-time arithmetic: a gap that
spans a clock change must still be exactly its nominal number of seconds, not
the wall-clock label arithmetic of aware-datetime + timedelta.
"""

from datetime import UTC, datetime

import pytest

from services.native.library_schedule_service import LibraryScheduleService

_LONDON = "Europe/London"


@pytest.mark.parametrize(
    ("terminal_utc", "expected_local"),
    [
        # Spring forward (2026-03-29 01:00 GMT -> 02:00 BST): a 6hr gap ending
        # after the jump lands at 04:00 BST == 03:00 UTC, exactly 21600s later.
        (datetime(2026, 3, 28, 21, 0, tzinfo=UTC), (4, 0)),
        # Fall back (2026-10-25 02:00 BST -> 01:00 GMT): a 6hr gap from 01:30 BST
        # lands at 06:30 GMT == 05:30 UTC, again exactly 21600s later.
    ],
)
def test_interval_due_tracks_true_elapsed_time_across_dst(
    terminal_utc: datetime, expected_local: tuple[int, int]
) -> None:
    terminal_at = terminal_utc.timestamp()
    due_instant = terminal_at + 21_600.0

    due = LibraryScheduleService.next_due(
        "6hr",
        "03:00",
        terminal_at,
        now=datetime.fromtimestamp(due_instant, UTC),
        timezone_name=_LONDON,
    )

    assert due is not None
    assert due.timestamp() == due_instant
    assert (due.hour, due.minute) == expected_local


def test_interval_due_before_any_transition_still_anchors_to_terminal() -> None:
    terminal_at = datetime(2026, 1, 15, 12, 0, tzinfo=UTC).timestamp()

    due = LibraryScheduleService.next_due(
        "1hr",
        "03:00",
        terminal_at,
        now=datetime.fromtimestamp(terminal_at + 3_600.0, UTC),
        timezone_name=_LONDON,
    )

    assert due is not None
    assert due.timestamp() == terminal_at + 3_600.0
