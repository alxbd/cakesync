"""Configuration: cakesync.yml for structure, .env for secrets.

Naming convention for YAML keys, so the schema stays predictable as it grows:

* ``caldav_*``  — describes the source calendar
* ``todoist_*`` — describes what we create in Todoist
* ``sync_*``    — describes sync behaviour itself

Units are always part of the key name (``_days``, ``_minutes_before``, ``_24h``).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

log = logging.getLogger("sync.config")

DEFAULT_CONFIG_PATH = "cakesync.yml"
TIME_24H_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_REMINDER_MINUTES = 1440  # 24h, the largest relative offset we accept
TITLE_STYLES = ("normal", "italic", "bold")
MULTI_DAY_MODES = ("rolling", "first_day", "none")

# Every key a `calendars` entry must carry. There is deliberately no defaults
# block and no inheritance: a calendar entry states all of its own settings, so
# there is exactly one place to look for any value, and a key you forgot is an
# error rather than a silent fallback to something you never chose.
CALENDAR_KEYS: tuple[str, ...] = (
    "caldav_calendar_name",
    "sync_enabled",
    "sync_window_future_days",
    "sync_retain_after_end_hours",
    "sync_multi_day_mode",
    "todoist_project_name",
    "todoist_label",
    "todoist_task_emoji",
    "todoist_create_checkable_tasks",
    "todoist_task_title_style",
    "todoist_reminders_minutes_before",
    "todoist_all_day_reminder_time_24h",
)


class ConfigError(Exception):
    """Raised for any invalid configuration. Always fatal, never swallowed."""


@dataclass(frozen=True)
class BirthdayConfig:
    sync_enabled: bool = True
    todoist_project_name: str = "Birthdays"
    todoist_label: str = ""
    todoist_task_emoji: str = ""
    todoist_task_time_of_day_24h: str = ""
    todoist_reminders_minutes_before: list[int] = field(default_factory=list)
    todoist_task_title_style: str = "normal"
    todoist_create_checkable_tasks: bool = True


@dataclass(frozen=True)
class CalendarConfig:
    caldav_calendar_name: str
    todoist_project_name: str
    sync_enabled: bool = True
    sync_window_future_days: int = 90
    # Hours to keep an occurrence after its end time. 0 drops it as soon as it
    # finishes, which is what stops finished events sitting in Todoist's Overdue
    # list: any task with a past due date is overdue, and there is no way to mark
    # one as merely informational.
    sync_retain_after_end_hours: int = 0
    # How an event spanning several days shows up, since Todoist cannot display
    # a span: "rolling" keeps one task due today until the event ends,
    # "first_day" shows it on its first day only, "none" leaves it out.
    sync_multi_day_mode: str = "rolling"
    todoist_label: str = ""
    todoist_task_emoji: str = ""
    todoist_create_checkable_tasks: bool = True
    todoist_task_title_style: str = "normal"
    todoist_reminders_minutes_before: list[int] = field(default_factory=list)
    # Relative reminders never fire on all-day Todoist tasks. When this is set,
    # an all-day event instead gets an absolute reminder at this floating time
    # on the day it starts. Empty means all-day events get no reminder at all.
    todoist_all_day_reminder_time_24h: str = ""


@dataclass(frozen=True)
class Credentials:
    url: str
    username: str
    password: str


@dataclass(frozen=True)
class AppConfig:
    timezone: ZoneInfo
    birthdays: BirthdayConfig | None
    calendars: list[CalendarConfig]
    carddav: Credentials | None
    caldav: Credentials | None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{key}: expected true or false, got {value!r}")


def _as_positive_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{key}: expected a positive whole number, got {value!r}")
    return value


def _as_str(value: Any, key: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{key}: expected text, got {value!r}")
    return value.strip()


def _as_time_24h(value: Any, key: str) -> str:
    text = _as_str(value, key)
    if not text:
        return ""
    if not TIME_24H_RE.match(text):
        raise ConfigError(f"{key}: expected 24h HH:MM (e.g. 09:00), got {text!r}")
    return text


def _as_choice(value: Any, key: str, choices: tuple[str, ...]) -> str:
    """One of `choices`, case-insensitive. Empty means the first choice."""
    text = _as_str(value, key).lower()
    if not text:
        return choices[0]
    if text not in choices:
        raise ConfigError(f"{key}: expected one of {', '.join(choices)}, got {text!r}")
    return text


def _as_reminders(value: Any, key: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{key}: expected a list of minutes, e.g. [0, 60], got {value!r}")
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"{key}: reminder offsets must be whole minutes, got {item!r}")
        if not 0 <= item <= MAX_REMINDER_MINUTES:
            raise ConfigError(
                f"{key}: reminder offset {item} is outside 0..{MAX_REMINDER_MINUTES} minutes"
            )
        out.append(item)
    return sorted(set(out))


def _unknown_keys(entry: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}"
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_birthdays(raw: Any) -> BirthdayConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("birthdays: expected a block of keys")
    allowed = {
        "sync_enabled",
        "todoist_project_name",
        "todoist_label",
        "todoist_task_emoji",
        "todoist_task_time_of_day_24h",
        "todoist_reminders_minutes_before",
        "todoist_task_title_style",
        "todoist_create_checkable_tasks",
    }
    _unknown_keys(raw, allowed, "birthdays")

    project = _as_str(raw.get("todoist_project_name"), "birthdays.todoist_project_name")
    if not project:
        raise ConfigError("birthdays.todoist_project_name is required")

    time_of_day = _as_time_24h(
        raw.get("todoist_task_time_of_day_24h"), "birthdays.todoist_task_time_of_day_24h"
    )
    reminders = _as_reminders(
        raw.get("todoist_reminders_minutes_before"),
        "birthdays.todoist_reminders_minutes_before",
    )
    if reminders and not time_of_day:
        raise ConfigError(
            "birthdays.todoist_reminders_minutes_before needs "
            "birthdays.todoist_task_time_of_day_24h to be set "
            "(a relative reminder cannot fire on an all-day task)"
        )

    return BirthdayConfig(
        sync_enabled=_as_bool(raw.get("sync_enabled", True), "birthdays.sync_enabled"),
        todoist_project_name=project,
        todoist_label=_as_str(raw.get("todoist_label"), "birthdays.todoist_label"),
        todoist_task_emoji=_as_str(raw.get("todoist_task_emoji"), "birthdays.todoist_task_emoji"),
        todoist_task_time_of_day_24h=time_of_day,
        todoist_reminders_minutes_before=reminders,
        todoist_task_title_style=_as_choice(
            raw.get("todoist_task_title_style"), "birthdays.todoist_task_title_style", TITLE_STYLES
        ),
        todoist_create_checkable_tasks=_as_bool(
            raw.get("todoist_create_checkable_tasks", True),
            "birthdays.todoist_create_checkable_tasks",
        ),
    )


def _parse_calendar(raw: dict, where: str) -> CalendarConfig:
    _unknown_keys(raw, set(CALENDAR_KEYS), where)

    missing = [key for key in CALENDAR_KEYS if key not in raw]
    if missing:
        raise ConfigError(
            f"{where}: missing key(s) {', '.join(missing)}. Every calendar entry must "
            "state all of them; see cakesync.example.yml."
        )
    merged = raw

    name = _as_str(merged.get("caldav_calendar_name"), f"{where}.caldav_calendar_name")
    if not name:
        raise ConfigError(f"{where}.caldav_calendar_name must not be empty")
    project = _as_str(merged.get("todoist_project_name"), f"{where}.todoist_project_name")
    if not project:
        raise ConfigError(f"{where}.todoist_project_name must not be empty")

    return CalendarConfig(
        caldav_calendar_name=name,
        todoist_project_name=project,
        sync_enabled=_as_bool(merged["sync_enabled"], f"{where}.sync_enabled"),
        sync_window_future_days=_as_positive_int(
            merged["sync_window_future_days"], f"{where}.sync_window_future_days"
        ),
        sync_retain_after_end_hours=_as_positive_int(
            merged["sync_retain_after_end_hours"], f"{where}.sync_retain_after_end_hours"
        ),
        sync_multi_day_mode=_as_choice(
            merged["sync_multi_day_mode"], f"{where}.sync_multi_day_mode", MULTI_DAY_MODES
        ),
        todoist_label=_as_str(merged["todoist_label"], f"{where}.todoist_label"),
        todoist_task_emoji=_as_str(merged["todoist_task_emoji"], f"{where}.todoist_task_emoji"),
        todoist_create_checkable_tasks=_as_bool(
            merged["todoist_create_checkable_tasks"], f"{where}.todoist_create_checkable_tasks"
        ),
        todoist_task_title_style=_as_choice(
            merged["todoist_task_title_style"], f"{where}.todoist_task_title_style", TITLE_STYLES
        ),
        todoist_reminders_minutes_before=_as_reminders(
            merged["todoist_reminders_minutes_before"],
            f"{where}.todoist_reminders_minutes_before",
        ),
        todoist_all_day_reminder_time_24h=_as_time_24h(
            merged["todoist_all_day_reminder_time_24h"],
            f"{where}.todoist_all_day_reminder_time_24h",
        ),
    )


def parse_config(data: Any) -> tuple[BirthdayConfig | None, list[CalendarConfig]]:
    """Turn parsed YAML into validated config objects."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("cakesync.yml: top level must be a mapping")
    _unknown_keys(data, {"timezone", "birthdays", "calendars"}, "cakesync.yml")

    birthdays = _parse_birthdays(data.get("birthdays"))

    raw_list = data.get("calendars") or []
    if not isinstance(raw_list, list):
        raise ConfigError("calendars: expected a list of calendar entries")

    calendars: list[CalendarConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            raise ConfigError(f"calendars[{i}]: expected a block of keys")
        cal = _parse_calendar(entry, f"calendars[{i}]")
        key = cal.caldav_calendar_name.casefold()
        if key in seen:
            raise ConfigError(
                f"calendars: {cal.caldav_calendar_name!r} appears twice. "
                "One entry per source calendar; point both at the same "
                "todoist_project_name instead if that was the intent."
            )
        seen.add(key)
        calendars.append(cal)

    return birthdays, calendars


def parse_timezone(data: Any) -> ZoneInfo:
    """The timezone cakesync reasons in. Required.

    It decides what "today" is, when a day is over and whether a reminder time
    has passed, so it must match the Todoist account: Todoist uses that one to
    show a task as Overdue and to fire reminders. The machine's own timezone
    plays no part, so a server in UTC behaves like a laptop in Phnom Penh.
    """
    name = _as_str((data or {}).get("timezone"), "timezone")
    if not name:
        raise ConfigError(
            "timezone is required, e.g. timezone: Europe/Paris. "
            "Use the timezone of your Todoist account."
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"timezone: unknown timezone {name!r}, expected an IANA name such as Europe/Paris"
        ) from None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _credentials(prefix: str, default_url: str) -> Credentials | None:
    username = (os.environ.get(f"{prefix}_USERNAME") or "").strip()
    password = os.environ.get(f"{prefix}_PASSWORD") or ""
    if not username or not password:
        return None
    url = (os.environ.get(f"{prefix}_URL") or default_url).rstrip("/") + "/"
    return Credentials(url=url, username=username, password=password)


def load_config(path: str | None = None) -> AppConfig:
    """Load and validate cakesync.yml plus the credentials from the environment.

    Exits the process on any problem: a half-understood config is more dangerous
    than no run at all, because this tool deletes tasks.
    """
    config_path = Path(path or os.environ.get("CAKESYNC_CONFIG") or DEFAULT_CONFIG_PATH)

    if not config_path.is_file():
        log.error(
            "%s not found. Copy cakesync.example.yml to cakesync.yml and edit it. "
            "Credentials stay in .env; everything else lives in the YAML.",
            config_path,
        )
        sys.exit(1)

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        birthdays, calendars = parse_config(data)
        timezone = parse_timezone(data)
    except (ConfigError, yaml.YAMLError) as e:
        log.error("configuration error: %s", e)
        sys.exit(1)

    carddav = _credentials("CARDDAV", "https://carddav.fastmail.com/dav/")
    caldav = _credentials("CALDAV", "https://caldav.fastmail.com/dav/")

    if birthdays and birthdays.sync_enabled and carddav is None:
        log.error("birthdays.sync_enabled is true but CARDDAV_USERNAME/CARDDAV_PASSWORD are unset")
        sys.exit(1)
    if any(c.sync_enabled for c in calendars) and caldav is None:
        log.error(
            "calendars has enabled entries but CALDAV_USERNAME/CALDAV_PASSWORD are unset. "
            "Note that Fastmail scopes app passwords per service: the CardDAV password will "
            "not work for calendars, you need a separate one with Calendars access."
        )
        sys.exit(1)

    return AppConfig(
        timezone=timezone,
        birthdays=birthdays,
        calendars=calendars,
        carddav=carddav,
        caldav=caldav,
    )
