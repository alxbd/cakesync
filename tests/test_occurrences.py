"""Tests for iCalendar expansion and the occurrence-to-task mapping."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from caldav import LOOPBACK_UID_DOMAINS, Occurrence, _event_end, _is_loopback
from config import CalendarConfig
from sync import _duration, _occurrence_content, occurrence_tasks

PARIS = ZoneInfo("Europe/Paris")
PHNOM_PENH = ZoneInfo("Asia/Phnom_Penh")
# Before every occurrence used here, so none of them has started yet.
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def cfg(
    *,
    sync_multi_day_mode: str = "rolling",
    todoist_task_emoji: str = "",
    todoist_create_checkable_tasks: bool = True,
    todoist_reminders_minutes_before: list[int] | None = None,
    todoist_all_day_reminder_time_24h: str = "",
    todoist_task_title_style: str = "normal",
) -> CalendarConfig:
    return CalendarConfig(
        caldav_calendar_name="Personal",
        todoist_project_name="Agenda",
        sync_multi_day_mode=sync_multi_day_mode,
        todoist_task_emoji=todoist_task_emoji,
        todoist_create_checkable_tasks=todoist_create_checkable_tasks,
        todoist_reminders_minutes_before=todoist_reminders_minutes_before or [],
        todoist_all_day_reminder_time_24h=todoist_all_day_reminder_time_24h,
        todoist_task_title_style=todoist_task_title_style,
    )


def timed(start, end=None) -> Occurrence:
    return Occurrence(
        calendar_name="Personal",
        uid="uid-1",
        summary="Standup",
        location="",
        description="",
        start=start,
        end=end,
        all_day=False,
    )


def allday(start, end=None) -> Occurrence:
    return Occurrence(
        calendar_name="Personal",
        uid="uid-1",
        summary="Holiday",
        location="",
        description="",
        start=start,
        end=end,
        all_day=True,
    )


# -- occurrence ids ---------------------------------------------------------


def test_timed_occurrence_id_is_utc():
    occ = timed(datetime(2026, 8, 10, 14, 0, tzinfo=PARIS))
    assert occ.occurrence_id == "20260810T120000Z"  # Paris is UTC+2 in August


def test_occurrence_id_is_stable_across_source_timezones():
    paris = timed(datetime(2026, 8, 10, 14, 0, tzinfo=PARIS))
    utc = timed(datetime(2026, 8, 10, 12, 0, tzinfo=UTC))
    assert paris.occurrence_id == utc.occurrence_id


def test_all_day_occurrence_id_has_no_time():
    assert allday(date(2026, 8, 21)).occurrence_id == "20260821"


# -- duration ---------------------------------------------------------------


def test_timed_duration_in_minutes():
    occ = timed(datetime(2026, 8, 10, 14, 0, tzinfo=UTC), datetime(2026, 8, 10, 14, 30, tzinfo=UTC))
    assert _duration(occ) == {"duration": 30, "duration_unit": "minute"}


def test_duration_longer_than_a_day_is_dropped():
    start = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    assert _duration(timed(start, start + timedelta(days=3))) is None


def test_single_all_day_event_gets_no_duration():
    assert _duration(allday(date(2026, 8, 21), date(2026, 8, 22))) is None


def test_multi_day_all_day_event_gets_no_duration():
    # Todoist stores a duration in days, but no Todoist app displays it.
    assert _duration(allday(date(2026, 8, 21), date(2026, 8, 23))) is None


# -- DTEND vs DURATION ------------------------------------------------------


def test_duration_property_is_used_when_dtend_is_absent():
    class Prop:
        def __init__(self, dt):
            self.dt = dt

    component = {"DURATION": Prop(timedelta(days=2))}
    assert _event_end(component, date(2026, 8, 21), True) == date(2026, 8, 23)


def test_all_day_without_end_defaults_to_one_day():
    assert _event_end({}, date(2026, 8, 21), True) == date(2026, 8, 22)


# -- loopback guard ---------------------------------------------------------


def test_todoist_uids_are_treated_as_loopback():
    assert "todoist.com" in LOOPBACK_UID_DOMAINS
    assert _is_loopback("2877280i-13152940-6fwjqCcchwvpp6RC-20260814@todoist.com")


def test_ordinary_uids_are_not_loopback():
    assert not _is_loopback("012cce3f-d5b9-4542-9ec6-0f69338a8355")
    assert not _is_loopback("something@nottodoist.com.example.org")


# -- task shape -------------------------------------------------------------


def test_non_checkable_prefix_comes_before_the_emoji():
    content = _occurrence_content(
        cfg(todoist_create_checkable_tasks=False, todoist_task_emoji="X"),
        timed(datetime(2026, 8, 10, 14, 0, tzinfo=UTC)),
        UTC,
    )
    assert content == "* X Standup"


def test_checkable_task_has_no_star():
    content = _occurrence_content(
        cfg(todoist_task_emoji="X"), timed(datetime(2026, 8, 10, 14, 0, tzinfo=UTC)), UTC
    )
    assert content == "X Standup"


def test_timed_event_gets_relative_reminders():
    tasks = occurrence_tasks(
        cfg(todoist_reminders_minutes_before=[0, 60], todoist_all_day_reminder_time_24h="09:00"),
        "p1",
        [timed(datetime(2026, 8, 10, 14, 0, tzinfo=PARIS))],
        NOW,
    )
    assert tasks[0].reminders_minutes_before == [0, 60]
    assert tasks[0].absolute_reminder is None
    assert tasks[0].due == {"due_datetime": "2026-08-10T12:00:00Z"}


def test_all_day_event_gets_an_absolute_reminder_instead():
    tasks = occurrence_tasks(
        cfg(todoist_reminders_minutes_before=[0], todoist_all_day_reminder_time_24h="09:00"),
        "p1",
        [allday(date(2026, 8, 21), date(2026, 8, 22))],
        NOW,
    )
    assert tasks[0].reminders_minutes_before == []
    assert tasks[0].absolute_reminder == "2026-08-21T09:00:00"
    assert tasks[0].due == {"due_date": "2026-08-21"}


def test_all_day_reminder_already_past_is_not_sent():
    late = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)
    tasks = occurrence_tasks(
        cfg(todoist_all_day_reminder_time_24h="09:00"),
        "p1",
        [allday(date(2026, 11, 10), date(2026, 11, 11))],
        late,
    )
    assert tasks[0].absolute_reminder is None


def test_all_day_event_without_configured_time_gets_no_reminder():
    tasks = occurrence_tasks(
        cfg(todoist_reminders_minutes_before=[0]), "p1", [allday(date(2026, 8, 21))], NOW
    )
    assert tasks[0].absolute_reminder is None
    assert tasks[0].reminders_minutes_before == []


def test_location_and_description_become_the_body():
    occ = Occurrence(
        calendar_name="Personal",
        uid="uid-1",
        summary="Standup",
        location="Room B",
        description="Bring the demo",
        start=datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
        end=None,
        all_day=False,
    )
    assert occurrence_tasks(cfg(), "p1", [occ], NOW)[0].body == ["Room B", "Bring the demo"]


# -- multi-day events -------------------------------------------------------


def workshop() -> Occurrence:
    """All-day, 10 to 13 Nov. An all-day DTEND is exclusive, hence the 14th."""
    return allday(date(2026, 11, 10), date(2026, 11, 14))


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=UTC)


def tasks_for(mode: str, occ: Occurrence, now: datetime):
    config = cfg(sync_multi_day_mode=mode, todoist_all_day_reminder_time_24h="09:00")
    return occurrence_tasks(config, "p1", [occ], now)


def test_multi_day_title_shows_the_last_day():
    assert _occurrence_content(cfg(), workshop(), UTC) == "Holiday (until 13 Nov)"


def test_single_day_title_has_no_suffix():
    single = allday(date(2026, 11, 10), date(2026, 11, 11))
    assert _occurrence_content(cfg(), single, UTC) == "Holiday"


def test_timed_event_past_midnight_is_not_multi_day():
    evening = timed(at(10, 22), datetime(2026, 11, 11, 1, 0, tzinfo=UTC))
    assert _occurrence_content(cfg(), evening, UTC) == "Standup"


def test_none_mode_skips_multi_day_events():
    assert tasks_for("none", workshop(), at(9)) == []


def test_none_mode_keeps_single_day_events():
    assert len(tasks_for("none", allday(date(2026, 11, 10), date(2026, 11, 11)), at(9))) == 1


def test_rolling_is_due_on_the_first_day_before_it_starts():
    task = tasks_for("rolling", workshop(), at(9))[0]
    assert task.due == {"due_date": "2026-11-10"}
    assert task.absolute_reminder == "2026-11-10T09:00:00"


def test_rolling_moves_to_today_while_the_event_runs():
    before = tasks_for("rolling", workshop(), at(9))[0]
    during = tasks_for("rolling", workshop(), at(12))[0]
    assert during.due == {"due_date": "2026-11-12"}
    assert during.absolute_reminder is None
    assert during.key == before.key  # the same task gets updated, not a new one


def test_rolling_timed_event_becomes_all_day_after_its_first_day():
    trip = timed(at(10, 9), datetime(2026, 11, 12, 17, 0, tzinfo=UTC))
    config = cfg(todoist_reminders_minutes_before=[0])
    first = occurrence_tasks(config, "p1", [trip], at(9))[0]
    later = occurrence_tasks(config, "p1", [trip], at(11))[0]
    assert first.due == {"due_datetime": "2026-11-10T09:00:00Z"}
    assert first.reminders_minutes_before == [0]
    assert later.due == {"due_date": "2026-11-11"}
    assert later.reminders_minutes_before == []


def test_first_day_mode_shows_it_on_the_first_day():
    task = tasks_for("first_day", workshop(), at(10, 6))[0]
    assert task.due == {"due_date": "2026-11-10"}


def test_first_day_mode_drops_it_after_the_first_day():
    assert tasks_for("first_day", workshop(), at(12)) == []


def test_the_day_ends_at_midnight_in_the_configured_timezone():
    # 20:00 UTC on 10 Nov is already 03:00 on 11 Nov in Phnom Penh, so the
    # first day is over there but not in UTC.
    instant = at(10, 20)
    assert tasks_for("first_day", workshop(), instant) != []
    assert tasks_for("first_day", workshop(), instant.astimezone(PHNOM_PENH)) == []


def test_all_day_reminder_time_is_read_in_the_configured_timezone():
    # 03:00 UTC on 10 Nov is 10:00 in Phnom Penh: a 09:00 reminder there has
    # already passed, while 09:00 UTC is still ahead.
    instant = at(10, 3)
    assert tasks_for("rolling", workshop(), instant)[0].absolute_reminder is not None
    assert (
        tasks_for("rolling", workshop(), instant.astimezone(PHNOM_PENH))[0].absolute_reminder
        is None
    )


# -- title style ------------------------------------------------------------


def _title(**kwargs):
    return _occurrence_content(cfg(**kwargs), timed(datetime(2026, 8, 10, 14, 0, tzinfo=UTC)), UTC)


def test_normal_style_leaves_the_title_bare():
    assert _title(todoist_task_emoji="X") == "X Standup"


def test_italic_uses_underscores_not_asterisks():
    # Asterisks would collide with the "* " non-completable prefix.
    assert _title(todoist_task_emoji="X", todoist_task_title_style="italic") == "X _Standup_"


def test_bold_wraps_in_double_asterisks():
    assert _title(todoist_task_emoji="X", todoist_task_title_style="bold") == "X **Standup**"


def test_style_applies_to_the_text_not_the_emoji():
    assert _title(todoist_task_emoji="📅", todoist_task_title_style="italic") == "📅 _Standup_"


def test_non_completable_prefix_stays_first_with_italic():
    content = _title(
        todoist_task_emoji="X",
        todoist_task_title_style="italic",
        todoist_create_checkable_tasks=False,
    )
    assert content == "* X _Standup_"
    assert content.startswith("* ")


def test_style_works_without_an_emoji():
    assert _title(todoist_task_title_style="bold") == "**Standup**"


# -- retention window -------------------------------------------------------


def test_retention_uses_the_event_end_not_a_day_boundary():
    """An event that finished this morning must not survive until midnight.

    A Todoist task with a past due date is Overdue for as long as it exists, so
    a calendar-day cutoff kept finished events sitting in Overdue all day.
    """
    from datetime import timedelta

    from config import CalendarConfig

    cal = CalendarConfig(
        caldav_calendar_name="Personal",
        todoist_project_name="Agenda",
        sync_retain_after_end_hours=0,
    )
    assert cal.sync_retain_after_end_hours == 0

    now = datetime(2026, 8, 7, 14, 0, tzinfo=UTC)
    window_start = now - timedelta(hours=cal.sync_retain_after_end_hours)
    finished_this_morning = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
    assert finished_this_morning < window_start, "must fall outside the window"


def test_retention_grace_keeps_a_recently_finished_event():
    from datetime import timedelta

    from config import CalendarConfig

    cal = CalendarConfig(
        caldav_calendar_name="Personal",
        todoist_project_name="Agenda",
        sync_retain_after_end_hours=24,
    )
    now = datetime(2026, 8, 7, 14, 0, tzinfo=UTC)
    window_start = now - timedelta(hours=cal.sync_retain_after_end_hours)
    finished_this_morning = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
    assert finished_this_morning > window_start
