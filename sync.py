"""Reconciliation: turn source objects into desired tasks, then diff against Todoist.

Reconciliation is done per Todoist project rather than per source, because
several calendars may target the same project. Tasks without a cakesync marker
are invisible here: never updated, never deleted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import markers
from caldav import CalendarFetchError, Occurrence, discover_calendars, fetch_occurrences
from carddav import Birthday, Contact
from config import AppConfig, BirthdayConfig, CalendarConfig
from dav import build_session
from todoist import TodoistClient

log = logging.getLogger("sync")

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
# Todoist apps only display a duration shorter than a day. A timed event longer
# than this gets no duration and counts as multi-day.
MAX_DURATION_MINUTES = 1440
# One calendar expanding past this many occurrences is legal but worth saying out loud.
VOLUME_WARNING_THRESHOLD = 200


@dataclass
class Stats:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped_completed: int = 0
    skipped_loopback: int = 0
    reminders_added: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"created={self.created} updated={self.updated} deleted={self.deleted} "
            f"unchanged={self.unchanged} skipped_completed={self.skipped_completed} "
            f"skipped_loopback={self.skipped_loopback} "
            f"reminders_added={self.reminders_added} errors={self.errors}"
        )


@dataclass
class DesiredTask:
    key: str
    kind: str  # "birthday" or "calendar"
    project_id: str
    content: str
    body: list[str]  # synced description lines below the marker
    marker: str
    labels: list[str] = field(default_factory=list)
    due: dict = field(default_factory=dict)
    duration: dict | None = None
    reminders_minutes_before: list[int] = field(default_factory=list)
    absolute_reminder: str | None = None


# ---------------------------------------------------------------------------
# Birthdays -> desired tasks
# ---------------------------------------------------------------------------


def _task_title(text: str, *, emoji: str, style: str, checkable: bool) -> str:
    """Assemble a task title: optional `* ` prefix, optional emoji, styled text.

    Todoist renders task content as Markdown. Italic uses `_text_` rather than
    `*text*` on purpose: a non-completable task already starts with `* `, and
    `* *text*` is ambiguous to a Markdown parser, while `* _text_` is not.

    The emoji stays outside the styling. Decorating it changes nothing visually
    and would only make the stored content harder to read.
    """
    styled = text
    if text:
        if style == "italic":
            styled = f"_{text}_"
        elif style == "bold":
            styled = f"**{text}**"
    title = f"{emoji} {styled}".strip()
    # The `* ` marker must be the very first thing in the content.
    return title if checkable else f"* {title}"


def _birthday_content(cfg: BirthdayConfig, contact: Contact) -> str:
    name = contact.name
    if contact.birthday.year is not None:
        name = f"{name} ({contact.birthday.year})"
    return _task_title(
        name,
        emoji=cfg.todoist_task_emoji,
        style=cfg.todoist_task_title_style,
        checkable=cfg.todoist_create_checkable_tasks,
    )


def _birthday_due_string(bday: Birthday, time_of_day: str) -> str:
    base = f"every {bday.day} {MONTHS[bday.month - 1]}"
    return f"{base} at {time_of_day}" if time_of_day else base


def birthday_tasks(
    cfg: BirthdayConfig, project_id: str, contacts: list[Contact]
) -> list[DesiredTask]:
    out: list[DesiredTask] = []
    for contact in contacts:
        out.append(
            DesiredTask(
                key=contact.uid,
                kind="birthday",
                project_id=project_id,
                content=_birthday_content(cfg, contact),
                body=[],
                marker=markers.birthday_marker(contact.uid),
                labels=[cfg.todoist_label] if cfg.todoist_label else [],
                due={
                    "due_string": _birthday_due_string(
                        contact.birthday, cfg.todoist_task_time_of_day_24h
                    ),
                    "due_lang": "en",
                },
                reminders_minutes_before=list(cfg.todoist_reminders_minutes_before),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Calendar occurrences -> desired tasks
# ---------------------------------------------------------------------------


def _local_day(value: datetime | date) -> date:
    return value.astimezone().date() if isinstance(value, datetime) else value


def _last_day(occ: Occurrence) -> date | None:
    """The last day a multi-day occurrence covers, or None for a single-day one.

    An all-day DTEND is exclusive: a 10-13 Nov event ends on 14 Nov. A timed
    event is multi-day only past 24h, so 22:00 to 01:00 stays a single evening.
    """
    if occ.end is None:
        return None
    if occ.all_day:
        last = _local_day(occ.end) - timedelta(days=1)
        return last if last > _local_day(occ.start) else None
    if occ.end - occ.start <= timedelta(minutes=MAX_DURATION_MINUTES):
        return None
    return _local_day(occ.end - timedelta(seconds=1))


def _occurrence_content(cfg: CalendarConfig, occ: Occurrence) -> str:
    text = occ.summary
    last = _last_day(occ)
    if last is not None:
        text = f"{text} (until {last.day} {MONTHS[last.month - 1][:3]})"
    return _task_title(
        text,
        emoji=cfg.todoist_task_emoji,
        style=cfg.todoist_task_title_style,
        checkable=cfg.todoist_create_checkable_tasks,
    )


def _duration(occ: Occurrence) -> dict | None:
    # All-day tasks get none: Todoist stores a duration in days but no app shows
    # it. Multi-day events are handled by sync_multi_day_mode instead.
    if occ.end is None or occ.all_day:
        return None
    minutes = int((occ.end - occ.start).total_seconds() // 60)
    if minutes <= 0:
        return None
    if minutes > MAX_DURATION_MINUTES:
        log.debug(
            "%s: %r runs %d minutes, longer than Todoist's 24h maximum — no duration set",
            occ.calendar_name,
            occ.summary,
            minutes,
        )
        return None
    return {"duration": minutes, "duration_unit": "minute"}


def occurrence_tasks(
    cfg: CalendarConfig, project_id: str, occurrences: list[Occurrence], now: datetime
) -> list[DesiredTask]:
    today = now.astimezone().date()
    mode = cfg.sync_multi_day_mode
    out: list[DesiredTask] = []
    for occ in occurrences:
        multi_day = _last_day(occ) is not None
        if multi_day and mode == "none":
            log.debug("%s: %r spans several days, skipped", occ.calendar_name, occ.summary)
            continue
        if multi_day and mode == "first_day":
            # Treat the event as ending with its first day, so the task never
            # sits in Overdue on the days after.
            first_day_end = datetime.combine(_local_day(occ.start) + timedelta(days=1), time())
            retain = timedelta(hours=cfg.sync_retain_after_end_hours)
            if first_day_end.astimezone() + retain <= now:
                continue

        key = markers.calendar_key(occ.calendar_name, occ.uid, occ.occurrence_id)
        body = [line for line in (occ.location, occ.description) if line]
        marker = markers.calendar_marker(occ.calendar_name, occ.uid, occ.occurrence_id, body)

        if multi_day and mode == "rolling" and today > _local_day(occ.start):
            # Keep the running event on today rather than letting it go Overdue.
            # Its reminders belonged to the first day and have already fired.
            due = {"due_date": today.isoformat()}
            relative: list[int] = []
            absolute = None
        elif occ.all_day:
            due = {"due_date": occ.start.strftime("%Y-%m-%d")}
            relative = []
            absolute = (
                f"{occ.start.strftime('%Y-%m-%d')}T{cfg.todoist_all_day_reminder_time_24h}:00"
                if cfg.todoist_all_day_reminder_time_24h
                else None
            )
        else:
            due = {"due_datetime": occ.start_utc_iso}
            relative = list(cfg.todoist_reminders_minutes_before)
            absolute = None
        if absolute and datetime.fromisoformat(absolute).astimezone() <= now:
            # Todoist accepts a reminder in the past but does not keep it, so
            # sending one would repeat on every run for the rest of the day.
            absolute = None

        out.append(
            DesiredTask(
                key=key,
                kind="calendar",
                project_id=project_id,
                content=_occurrence_content(cfg, occ),
                body=body,
                marker=marker,
                labels=[cfg.todoist_label] if cfg.todoist_label else [],
                due=due,
                duration=_duration(occ),
                reminders_minutes_before=relative,
                absolute_reminder=absolute,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Comparing against what Todoist already has
# ---------------------------------------------------------------------------


def _existing_due_instant(due: dict) -> datetime | None:
    """Resolve a Todoist due object to a UTC instant, or None if it is all-day."""
    raw = due.get("date") or ""
    if len(raw) < 11 or raw[10:11] != "T":
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        tz_name = due.get("timezone")
        if tz_name:
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
            except Exception:
                # Returning None here would make the due compare unequal forever
                # and rewrite the task on every run. Fall back to local time and
                # say so, rather than looping in silence.
                log.warning(
                    "Todoist returned an unrecognised timezone %r; comparing in local time",
                    tz_name,
                )
                parsed = parsed.astimezone()
        else:
            parsed = parsed.astimezone()
    return parsed.astimezone(UTC)


def _due_matches(task: dict, desired: DesiredTask) -> bool:
    due = task.get("due") or {}
    raw = due.get("date") or ""

    if "due_string" in desired.due:  # recurring birthday
        if not due.get("is_recurring") or len(raw) < 10:
            return False
        wanted = desired.due["due_string"]
        month_day = raw[5:10]
        try:
            month = int(month_day[:2])
            day = int(month_day[3:])
        except ValueError:
            return False
        if f"every {day} {MONTHS[month - 1]}" not in wanted:
            return False
        has_time = raw[10:11] == "T"
        wants_time = " at " in wanted
        if has_time != wants_time:
            return False
        if wants_time:
            if due.get("timezone"):  # birthdays are always floating
                return False
            return raw[11:16] == wanted.rsplit(" at ", 1)[1]
        return True

    if "due_date" in desired.due:
        return raw[:10] == desired.due["due_date"] and raw[10:11] != "T"

    wanted_instant = datetime.fromisoformat(desired.due["due_datetime"].replace("Z", "+00:00"))
    actual = _existing_due_instant(due)
    return actual is not None and actual == wanted_instant.astimezone(UTC)


def _duration_matches(task: dict, desired: DesiredTask) -> bool:
    actual = task.get("duration")
    if desired.duration is None:
        return not actual
    if not actual:
        return False
    return (
        actual.get("amount") == desired.duration["duration"]
        and actual.get("unit") == desired.duration["duration_unit"]
    )


def _labels_match(task: dict, desired: DesiredTask) -> bool:
    """True when our labels are present. Labels we did not add are not ours.

    A strict set comparison would strip any label the user added by hand on the
    next run. Changing todoist_label in the config therefore leaves the old one
    attached; removing it is the user's call, not ours.
    """
    return set(desired.labels) <= set(task.get("labels") or [])


def _merged_labels(task: dict, desired: DesiredTask) -> list[str]:
    return sorted(set(task.get("labels") or []) | set(desired.labels))


def _description_for(desired: DesiredTask, existing: dict | None) -> str:
    notes = markers.user_notes(existing.get("description") or "") if existing else ""
    return markers.build_description(notes, desired.marker, desired.body)


def _due_label(due: dict) -> str:
    """The desired due date for log lines, timed ones in this machine's local time."""
    if "due_string" in due:
        return due["due_string"]
    if "due_date" in due:
        return due["due_date"]
    instant = datetime.fromisoformat(due["due_datetime"].replace("Z", "+00:00"))
    return instant.astimezone().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Reconcile one project
# ---------------------------------------------------------------------------


def reconcile_project(
    client: TodoistClient,
    project_id: str,
    project_name: str,
    desired: list[DesiredTask],
    *,
    synced_kinds: set[str],
    allow_delete: bool,
    dry_run: bool,
    stats: Stats,
    auto_reminder_offset: int | None = None,
    reminders: list[dict] | None = None,
) -> None:
    """Reconcile one project.

    `synced_kinds` lists which sources actually ran and succeeded for this
    project ("birthday", "calendar"). Only those kinds are eligible for
    deletion. Inferring this from the desired list instead would delete every
    birthday task in a shared project during `--only calendars`, and would
    never prune a calendar that legitimately emptied.
    """
    prefix = "[dry-run] " if dry_run else ""
    desired_by_key = {(d.kind, d.key): d for d in desired}

    existing_tasks = client.list_tasks(project_id)
    existing_by_key: dict[tuple[str, str], dict] = {}
    untagged = 0
    for task in existing_tasks:
        description = task.get("description") or ""
        cal_key = markers.find_calendar_key(description)
        if cal_key:
            existing_by_key[("calendar", cal_key)] = task
            continue
        bday_uid = markers.find_birthday_uid(description)
        if bday_uid:
            existing_by_key[("birthday", bday_uid)] = task
            continue
        untagged += 1

    # A completed event task no longer appears in the active listing, so without
    # this it would be recreated on every run until it aged out of the window.
    completed_keys: set[tuple[str, str]] = set()
    if "calendar" in synced_kinds:
        for task in client.list_completed_tasks(project_id):
            key = markers.find_calendar_key(task.get("description") or "")
            if key:
                completed_keys.add(("calendar", key))

    log.info(
        "  %s%s: %d tagged, %d untagged (ignored), %d desired",
        prefix,
        project_name,
        len(existing_by_key),
        untagged,
        len(desired_by_key),
    )

    # Reminders are account-wide, so the caller fetches them once for the whole
    # run rather than re-sweeping them for every project.
    reminders_by_task: dict[str, set[tuple]] = {}
    for rem in reminders if reminders is not None else client.list_reminders():
        item = rem.get("item_id")
        if not item:
            continue
        if rem.get("type") == "relative":
            signature = ("relative", rem.get("minute_offset"))
        else:
            signature = ("absolute", ((rem.get("due") or {}).get("date") or "")[:16])
        reminders_by_task.setdefault(str(item), set()).add(signature)

    def ensure_reminders(task_id: str | None, task: DesiredTask, *, just_created: bool) -> None:
        wanted: list[tuple] = [("relative", m) for m in task.reminders_minutes_before]
        if task.absolute_reminder:
            wanted.append(("absolute", task.absolute_reminder[:16]))
        if not wanted:
            return
        have = reminders_by_task.get(task_id or "", set())
        if just_created and auto_reminder_offset is not None and task.reminders_minutes_before:
            # Todoist has already attached its own relative reminder at the
            # user's "automatic reminders" offset. Adding ours would duplicate it.
            have = have | {("relative", auto_reminder_offset)}
            reminders_by_task.setdefault(task_id or "", set()).add(
                ("relative", auto_reminder_offset)
            )
        for signature in wanted:
            if signature in have:
                continue
            if dry_run:
                log.info("  %s  + reminder %s", prefix, signature)
                stats.reminders_added += 1
                continue
            if not task_id:
                continue
            if signature[0] == "relative":
                client.add_relative_reminder(task_id, int(signature[1]))
            else:
                client.add_absolute_reminder(task_id, str(task.absolute_reminder))
            reminders_by_task.setdefault(task_id, set()).add(signature)
            stats.reminders_added += 1

    for key, want in desired_by_key.items():
        existing = existing_by_key.get(key)

        if existing is None:
            if key in completed_keys:
                stats.skipped_completed += 1
                continue
            log.info("  %s+ create: %s (%s)", prefix, want.content, _due_label(want.due))
            payload = {
                "content": want.content,
                "description": _description_for(want, None),
                "project_id": project_id,
                **want.due,
            }
            if want.labels:
                payload["labels"] = want.labels
            if want.duration:
                payload.update(want.duration)
            new_id = client.create_task(payload)
            stats.created += 1
            ensure_reminders(new_id, want, just_created=True)
            continue

        task_id = str(existing["id"])
        changed_content = existing.get("content") != want.content
        changed_due = not _due_matches(existing, want)
        changed_duration = not _duration_matches(existing, want)
        changed_labels = not _labels_match(existing, want)
        wanted_description = _description_for(want, existing)
        # Birthday descriptions are never rewritten: they carry only the marker,
        # and everything else in them belongs to the user.
        #
        # For calendar tasks we compare fingerprints rather than text. Todoist
        # rewrites what it stores (a bare URL becomes a markdown link), so the
        # text never matches what we sent and the task would be updated forever.
        changed_description = want.kind == "calendar" and markers.find_body_fingerprint(
            existing.get("description") or ""
        ) != markers.body_fingerprint(want.body)

        if (
            changed_content
            or changed_due
            or changed_duration
            or changed_labels
            or changed_description
        ):
            log.info("  %s~ update: %s (%s)", prefix, want.content, _due_label(want.due))
            payload: dict = {"content": want.content, **want.due}
            if "due_string" in want.due:
                payload["due_lang"] = "en"
            if changed_labels:
                payload["labels"] = _merged_labels(existing, want)
            if want.duration:
                payload.update(want.duration)
            elif changed_duration:
                payload["duration"] = None
            if changed_description:
                payload["description"] = wanted_description
            client.update_task(task_id, payload)
            stats.updated += 1
        else:
            stats.unchanged += 1

        ensure_reminders(task_id, want, just_created=False)

    if not allow_delete:
        log.warning(
            "  %s%s: skipping deletions, a source feeding this project failed to load",
            prefix,
            project_name,
        )
        return

    for key, task in existing_by_key.items():
        if key in desired_by_key:
            continue
        if key[0] not in synced_kinds:
            # No source of this kind ran for this project, so we know nothing
            # about whether these tasks are still wanted. Leave them alone.
            continue
        log.info(
            "  %s- delete: %s (%s)",
            prefix,
            task.get("content"),
            (task.get("due") or {}).get("string") or "no date",
        )
        client.delete_task(str(task["id"]))
        stats.deleted += 1


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def collect_calendar_tasks(
    config: AppConfig,
    project_ids: dict[str, str],
    stats: Stats,
) -> tuple[dict[str, list[DesiredTask]], set[str], set[str]]:
    """Fetch and expand every enabled calendar.

    Returns three things: the desired tasks grouped by project id; the project
    ids that must NOT be pruned because one of their sources failed (treating a
    failed fetch as "no events" would delete months of tasks); and the project
    ids for which at least one calendar was read successfully, which is what
    makes it safe to delete stale calendar tasks there.
    """
    by_project: dict[str, list[DesiredTask]] = {}
    blocked: set[str] = set()
    synced: set[str] = set()

    enabled = [c for c in config.calendars if c.sync_enabled]
    if not enabled or config.caldav is None:
        return by_project, blocked, synced

    session = build_session(config.caldav.username, config.caldav.password)
    try:
        available = discover_calendars(session, config.caldav.url)
    except Exception as e:
        log.error("CalDAV: calendar discovery failed: %s", e)
        stats.errors += 1
        return by_project, {project_ids[c.todoist_project_name] for c in enabled}, synced

    log.info("  %d calendar(s) on the server: %s", len(available), ", ".join(sorted(available)))

    now = datetime.now(UTC)
    for cfg in enabled:
        project_id = project_ids[cfg.todoist_project_name]
        url = available.get(cfg.caldav_calendar_name)
        if url is None:
            log.error(
                "calendar %r not found on the server. Available: %s",
                cfg.caldav_calendar_name,
                ", ".join(sorted(repr(n) for n in available)),
            )
            stats.errors += 1
            blocked.add(project_id)
            continue

        # Cut on the event's own end time, not on a calendar-day boundary. A
        # midnight cutoff would keep an event that finished this morning until
        # tomorrow, and a task with a past due date is Overdue in Todoist for as
        # long as it exists. Expansion returns occurrences overlapping the
        # window, so a multi-day event still in progress is kept, and
        # sync_multi_day_mode decides how it shows up.
        window_start = now - timedelta(hours=cfg.sync_retain_after_end_hours)
        window_end = now + timedelta(days=cfg.sync_window_future_days)

        try:
            occurrences, skipped = fetch_occurrences(
                session, url, cfg.caldav_calendar_name, window_start, window_end
            )
        except CalendarFetchError as e:
            log.error("%s — not touching %r this run", e, cfg.todoist_project_name)
            stats.errors += 1
            blocked.add(project_id)
            continue

        stats.skipped_loopback += skipped
        if len(occurrences) > VOLUME_WARNING_THRESHOLD:
            log.warning(
                "calendar %r expands to %d occurrences over %d days",
                cfg.caldav_calendar_name,
                len(occurrences),
                cfg.sync_window_future_days,
            )
        log.info(
            "  %s: %d occurrence(s)%s",
            cfg.caldav_calendar_name,
            len(occurrences),
            f", {skipped} skipped as loopback" if skipped else "",
        )
        synced.add(project_id)
        by_project.setdefault(project_id, []).extend(
            occurrence_tasks(cfg, project_id, occurrences, now)
        )

    return by_project, blocked, synced


def desired_labels(config: AppConfig) -> set[str]:
    labels = {c.todoist_label for c in config.calendars if c.sync_enabled and c.todoist_label}
    if config.birthdays and config.birthdays.sync_enabled and config.birthdays.todoist_label:
        labels.add(config.birthdays.todoist_label)
    return labels


def project_names(config: AppConfig) -> set[str]:
    names = {c.todoist_project_name for c in config.calendars if c.sync_enabled}
    if config.birthdays and config.birthdays.sync_enabled:
        names.add(config.birthdays.todoist_project_name)
    return names


def name_for_id(project_ids: dict[str, str], project_id: str) -> str:
    return next((n for n, i in project_ids.items() if i == project_id), project_id)
