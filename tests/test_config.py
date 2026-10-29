"""Tests for cakesync.yml parsing and validation."""

import pytest
import yaml

from config import ConfigError, parse_config

COMPLETE = """
calendars:
  - caldav_calendar_name: Personal
    sync_enabled: true
    sync_window_future_days: 90
    sync_retain_after_end_hours: 0
    sync_multi_day_mode: rolling
    todoist_project_name: Agenda
    todoist_label: ""
    todoist_task_emoji: "X"
    todoist_create_checkable_tasks: true
    todoist_task_title_style: normal
    todoist_reminders_minutes_before: [15]
    todoist_all_day_reminder_time_24h: ""
"""

SECOND = """  - caldav_calendar_name: {name}
    sync_enabled: true
    sync_window_future_days: 90
    sync_retain_after_end_hours: 0
    sync_multi_day_mode: none
    todoist_project_name: {project}
    todoist_label: ""
    todoist_task_emoji: ""
    todoist_create_checkable_tasks: true
    todoist_task_title_style: normal
    todoist_reminders_minutes_before: []
    todoist_all_day_reminder_time_24h: ""
"""


def load(text):
    return parse_config(yaml.safe_load(text))


def entry(**overrides):
    """A complete calendars block with the given keys replaced."""
    lines = COMPLETE.rstrip().splitlines()
    for key, value in overrides.items():
        lines = [
            f"    {key}: {value}" if line.strip().startswith(f"{key}:") else line for line in lines
        ]
    return "\n".join(lines) + "\n"


# -- no inheritance ---------------------------------------------------------


def test_a_complete_entry_parses():
    _, calendars = load(COMPLETE)
    assert calendars[0].todoist_task_emoji == "X"
    assert calendars[0].sync_window_future_days == 90


def test_every_key_is_required():
    incomplete = "\n".join(
        line for line in COMPLETE.splitlines() if "todoist_task_emoji" not in line
    )
    with pytest.raises(ConfigError, match=r"missing key\(s\) todoist_task_emoji"):
        load(incomplete)


def test_the_error_lists_all_missing_keys():
    with pytest.raises(ConfigError, match="missing key") as excinfo:
        load("calendars:\n  - caldav_calendar_name: Personal\n")
    assert "todoist_project_name" in str(excinfo.value)
    assert "sync_window_future_days" in str(excinfo.value)


def test_a_defaults_block_is_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        load("calendar_defaults:\n  sync_enabled: true\n" + COMPLETE)


# -- values -----------------------------------------------------------------


def test_project_name_must_not_be_empty():
    with pytest.raises(ConfigError, match="todoist_project_name must not be empty"):
        load(entry(todoist_project_name='""'))


def test_duplicate_calendar_is_rejected():
    with pytest.raises(ConfigError, match="appears twice"):
        load(COMPLETE + SECOND.format(name="Personal", project="Other"))


def test_two_calendars_may_share_a_project():
    _, calendars = load(COMPLETE + SECOND.format(name="Doudouz", project="Agenda"))
    assert {c.todoist_project_name for c in calendars} == {"Agenda"}


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        load(COMPLETE + "    todoist_colour: red\n")


def test_reminder_offsets_are_bounded():
    with pytest.raises(ConfigError, match=r"outside 0\.\.1440"):
        load(entry(todoist_reminders_minutes_before="[10081]"))


def test_reminder_offsets_are_deduped_and_sorted():
    _, calendars = load(entry(todoist_reminders_minutes_before="[60, 0, 60]"))
    assert calendars[0].todoist_reminders_minutes_before == [0, 60]


def test_bad_time_format_is_rejected():
    with pytest.raises(ConfigError, match="expected 24h HH:MM"):
        load(entry(todoist_all_day_reminder_time_24h="'9am'"))


def test_negative_window_is_rejected():
    with pytest.raises(ConfigError, match="positive whole number"):
        load(entry(sync_retain_after_end_hours="-1"))


def test_unknown_title_style_is_rejected():
    with pytest.raises(ConfigError, match="expected one of normal, italic, bold"):
        load(entry(todoist_task_title_style="underline"))


def test_unknown_multi_day_mode_is_rejected():
    with pytest.raises(ConfigError, match="expected one of rolling, first_day, none"):
        load(entry(sync_multi_day_mode="span"))


def test_multi_day_mode_is_parsed():
    _, calendars = load(entry(sync_multi_day_mode="first_day"))
    assert calendars[0].sync_multi_day_mode == "first_day"


def test_title_style_is_case_insensitive():
    _, calendars = load(entry(todoist_task_title_style="Italic"))
    assert calendars[0].todoist_task_title_style == "italic"


# -- birthdays --------------------------------------------------------------


def test_birthday_reminder_needs_a_time_of_day():
    text = """
birthdays:
  todoist_project_name: Birthdays
  todoist_reminders_minutes_before: [0]
"""
    with pytest.raises(ConfigError, match="todoist_task_time_of_day_24h"):
        load(text)


def test_birthdays_accept_checkable_and_style():
    birthdays, _ = load("""
birthdays:
  todoist_project_name: Birthdays
  todoist_task_title_style: bold
  todoist_create_checkable_tasks: false
""")
    assert birthdays.todoist_task_title_style == "bold"
    assert birthdays.todoist_create_checkable_tasks is False


def test_birthdays_only_require_a_project_name():
    birthdays, calendars = load("birthdays:\n  todoist_project_name: Birthdays\n")
    assert birthdays.todoist_project_name == "Birthdays"
    assert calendars == []
