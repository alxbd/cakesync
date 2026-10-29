"""cakesync — sync CardDAV birthdays and CalDAV calendars into Todoist.

Works with any CardDAV/CalDAV-compliant server (Fastmail, iCloud, Nextcloud,
Radicale, Baïkal, SOGo…). Stateless: every task carries a `cakesync:` marker
inside its description, so the mapping is rebuilt at each run and the script
decides what to create, update or delete from that alone.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

import sync as sync_mod
from carddav import fetch_contacts
from config import load_config
from todoist import TodoistClient, ensure_labels, resolve_project_ids

log = logging.getLogger("sync")


def _setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = Path(os.environ.get("LOG_FILE", "logs/sync.log")).resolve()
    max_bytes = int(os.environ.get("LOG_MAX_BYTES", 1_048_576))  # 1 MiB
    backup_count = int(os.environ.get("LOG_BACKUP_COUNT", 5))

    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync CardDAV birthdays and CalDAV calendars into Todoist projects.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read and diff everything, but do not create/update/delete anything",
    )
    parser.add_argument(
        "--only",
        choices=("birthdays", "calendars"),
        default=None,
        help="run just one of the two sources",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to cakesync.yml (default: ./cakesync.yml or $CAKESYNC_CONFIG)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="override the LOG_LEVEL env (DEBUG, INFO, WARNING, ERROR)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    load_dotenv()
    if args.log_level:
        os.environ["LOG_LEVEL"] = args.log_level
    _setup_logging()

    prefix = "[dry-run] " if args.dry_run else ""
    config = load_config(args.config)

    do_birthdays = args.only != "calendars" and config.birthdays and config.birthdays.sync_enabled
    do_calendars = args.only != "birthdays" and any(c.sync_enabled for c in config.calendars)

    token = os.environ.get("TODOIST_API_TOKEN")
    if not token:
        log.error("environment variable TODOIST_API_TOKEN is required")
        sys.exit(1)

    client = TodoistClient(token, dry_run=args.dry_run)
    stats = sync_mod.Stats()
    auto_reminder_offset = client.auto_reminder_offset()

    names = sync_mod.project_names(config)
    if args.only == "birthdays" and config.birthdays:
        names = {config.birthdays.todoist_project_name}
    elif args.only == "calendars":
        names = {c.todoist_project_name for c in config.calendars if c.sync_enabled}
    project_ids = resolve_project_ids(client, names)
    ensure_labels(client, sync_mod.desired_labels(config), prefix=prefix)

    desired_by_project: dict[str, list[sync_mod.DesiredTask]] = {}
    blocked: set[str] = set()
    # Which source kinds actually ran and succeeded, per project. Only these are
    # eligible for deletion there; see reconcile_project.
    synced_kinds: dict[str, set[str]] = {}

    if do_birthdays and config.birthdays and config.carddav:
        cfg = config.birthdays
        project_id = project_ids[cfg.todoist_project_name]
        log.info("%sFetching contacts from %s ...", prefix, config.carddav.url)
        try:
            contacts = fetch_contacts(
                config.carddav.url, config.carddav.username, config.carddav.password
            )
            log.info("  %d contact(s) with a birthday", len(contacts))
            desired_by_project.setdefault(project_id, []).extend(
                sync_mod.birthday_tasks(cfg, project_id, contacts)
            )
            synced_kinds.setdefault(project_id, set()).add("birthday")
        except Exception as e:
            log.error("CardDAV: %s — not touching %r this run", e, cfg.todoist_project_name)
            stats.errors += 1
            blocked.add(project_id)

    if do_calendars and config.caldav:
        log.info("%sFetching calendars from %s ...", prefix, config.caldav.url)
        cal_tasks, cal_blocked, cal_synced = sync_mod.collect_calendar_tasks(
            config, project_ids, stats
        )
        for project_id, tasks in cal_tasks.items():
            desired_by_project.setdefault(project_id, []).extend(tasks)
        for project_id in cal_synced:
            synced_kinds.setdefault(project_id, set()).add("calendar")
        blocked |= cal_blocked

    # Reminders are account-wide; fetch them once rather than per project.
    reminders = client.list_reminders()

    for project_id in sorted(set(desired_by_project) | blocked | set(synced_kinds)):
        sync_mod.reconcile_project(
            client,
            project_id,
            sync_mod.name_for_id(project_ids, project_id),
            desired_by_project.get(project_id, []),
            synced_kinds=synced_kinds.get(project_id, set()),
            allow_delete=project_id not in blocked,
            dry_run=args.dry_run,
            stats=stats,
            tz=config.timezone,
            auto_reminder_offset=auto_reminder_offset,
            reminders=reminders,
        )

    log.info("%sDone. %s", prefix, stats.summary())
    if stats.errors:
        log.error("%d source(s) failed — see the log above", stats.errors)
        sys.exit(1)


if __name__ == "__main__":
    main()
