# cakesync 🎂

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Ruff](https://img.shields.io/badge/linter-ruff-261230.svg)
![Type checker: ty](https://img.shields.io/badge/typecheck-ty-261230.svg)

Sync contact birthdays from any CardDAV address book, and events from any CalDAV calendar, into Todoist projects.

Each run reconciles the target projects: creating missing tasks, updating changed ones, deleting tasks whose source is gone. No local state file — the source of truth is the address book, the calendars, and the tasks themselves.

- **Birthdays** become yearly recurring tasks, one per contact, forever.
- **Calendar events** become one task per occurrence inside a moving window (by default 90 days ahead, and dropped the moment the event ends). Recurring events are expanded locally, so moved and cancelled single occurrences are handled correctly.

## Compatible services

Works with any CardDAV/CalDAV-compliant server that accepts HTTP Basic Auth and implements the standard discovery flow (RFC 6352 and RFC 4791):

| Service | Status | Notes |
|---|---|---|
| **Fastmail** | ✅ Tested | App-specific passwords are **scoped per service**: CardDAV and CalDAV each need their own |
| **iCloud** | ✅ Expected | App-specific password required |
| **Nextcloud** / **ownCloud** | ✅ Expected | Same base URL for both protocols |
| **Radicale**, **Baïkal**, **SOGo** | ✅ Expected | |
| **Synology** | ✅ Expected | |
| Google | ❌ Not supported | CardDAV shut down, CalDAV requires OAuth2 |

See `.env.example` for sample URLs.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env                  # credentials
cp cakesync.example.yml cakesync.yml  # what to sync, and where
uv sync
uv run cakesync --dry-run
uv run cakesync
```

Credentials live in `.env`, everything else in `cakesync.yml`. There is exactly one place for each: cakesync exits with an error if `cakesync.yml` is missing rather than guessing from environment variables.

### Environment variables

| Variable | Purpose |
|---|---|
| `CARDDAV_URL` `CARDDAV_USERNAME` `CARDDAV_PASSWORD` | CardDAV, for birthdays. Use a **read-only** app password if your provider supports scoped credentials. |
| `CALDAV_URL` `CALDAV_USERNAME` `CALDAV_PASSWORD` | CalDAV, for calendars. Only needed when `calendars` is non-empty. |
| `TODOIST_API_TOKEN` | From *Settings → Integrations → Developer* in Todoist |
| `CAKESYNC_CONFIG` | Optional. Path to the config file, default `./cakesync.yml`. |
| `LOG_LEVEL` `LOG_FILE` `LOG_MAX_BYTES` `LOG_BACKUP_COUNT` | Optional logging settings |

On Fastmail, one app password will not cover both protocols. A CardDAV-scoped password returns `401` on `caldav.fastmail.com`, and vice versa.

## Configuration

`cakesync.yml` has two top-level blocks. See `cakesync.example.yml` for a commented reference.

```yaml
birthdays:            # CardDAV contacts
calendars:            # one entry per source calendar
```

Key names follow one rule, so the schema stays predictable:

- `caldav_*` describes the source calendar
- `todoist_*` describes what gets created in Todoist
- `sync_*` describes sync behaviour itself
- units are always in the name: `_days`, `_minutes_before`, `_24h`

| Key | Where | Meaning |
|---|---|---|
| `sync_enabled` | both | Skip this source entirely when false |
| `sync_window_future_days` | calendar | How far ahead occurrences are materialised |
| `sync_retain_after_end_hours` | calendar | Hours to keep an occurrence after it ends. `0` drops it the moment it finishes. |
| `sync_multi_day_mode` | calendar | `rolling`, `first_day` or `none`. See [Multi-day events](#multi-day-events). |
| `caldav_calendar_name` | calendar | Display name as it appears on the server |
| `todoist_project_name` | both | Target project. It must already exist. |
| `todoist_label` | both | Label applied to every task from this source |
| `todoist_task_emoji` | both | Prefixed to the task title |
| `todoist_task_time_of_day_24h` | birthdays | Floating time of day, or all-day when empty |
| `todoist_create_checkable_tasks` | both | `false` emits `* Title`, a non-completable task |
| `todoist_task_title_style` | both | `normal`, `italic` or `bold`. Styles the title text, not the emoji. |
| `todoist_reminders_minutes_before` | both | List of offsets in minutes, `0` meaning "at due time" |
| `todoist_all_day_reminder_time_24h` | calendar | Absolute reminder time for all-day events, none when empty |

Several calendars may point at the same `todoist_project_name`.

There is no defaults block and no inheritance: every `calendars` entry must state every key. A missing key is a fatal error naming exactly what is absent, rather than a silent fallback to a value you never chose. The `birthdays` block is the exception, where only `todoist_project_name` is required.

## Task format

**Birthdays.** Content `🎂 <Full Name>`, with `(<year>)` appended when the birth year is known. Due `every 15 January`, yearly recurring, all-day unless `todoist_task_time_of_day_24h` is set. That time is *floating*: `09:00` stays `09:00` wherever you are. Birth-year placeholders emitted by Apple Contacts (`1604-MM-DD`) count as "no year". Accepted `BDAY` formats: `YYYY-MM-DD`, `YYYYMMDD`, `--MM-DD`, `--MMDD`, and the same with a trailing `T…` component.

**Calendar events.** Content `📅 <Summary>`. The event `LOCATION` and `DESCRIPTION` go into the description. Todoist has no location field, so this is where an address lands, exactly as Todoist's own calendar integration does it.

Unlike birthdays, event times are **not** floating. A timed event is converted to its real UTC instant, so a 14:00 Paris meeting shows as 14:00 in Paris and 20:00 if your Todoist timezone is Bangkok. Duration comes from `DTEND` or `DURATION`; events longer than 24h get no duration, since Todoist caps it there. All-day events become all-day tasks with no duration: Todoist stores a duration in days, but its apps do not display it.

### Multi-day events

An event covers several days when it is all-day and lasts two days or more, or when it is timed and lasts longer than 24 hours. A timed event from 22:00 to 01:00 stays a single-day event. Todoist cannot show one task across several days, so `sync_multi_day_mode` picks what happens:

| Mode | What you see in Todoist |
|---|---|
| `rolling` | One task, due on the first day. From the second day it moves to today, so it never shows as Overdue. It is deleted when the event ends. Its reminder fires on the first day only. |
| `first_day` | One task on the first day only, deleted when that day ends (plus `sync_retain_after_end_hours`). |
| `none` | No task. Run with `--log-level DEBUG` to list the events skipped. |

With `rolling` and `first_day`, the title ends with the last day of the event, for example `📅 Workshop (until 13 Nov)`. Because "today" decides where a rolling task sits, cakesync uses the local date of the machine it runs on, so run it in the same timezone as your Todoist account.

### Finished events and Overdue

A Todoist task with a past due date is Overdue for as long as it exists, and Todoist offers no "informational" date. So a finished event has to be removed, and `sync_retain_after_end_hours` controls when.

The cut is made against the event's own end time, not a calendar-day boundary. That matters: a midnight-based cutoff kept an event that finished at 09:00 sitting in Overdue for the rest of the day, no matter how often you ran cakesync.

With the default `0`, an occurrence is dropped as soon as it ends. **Your cron interval is then your worst-case Overdue exposure**, because the task only disappears on the next run: hourly cron means at most an hour. Set it to `24` if you would rather keep finished events around for a day and accept them showing as overdue.

Multi-day events still in progress are kept, since expansion returns occurrences overlapping the window rather than only those starting inside it. `sync_multi_day_mode` then decides how they show up.

Reminders: timed events get relative reminders from `todoist_reminders_minutes_before`. Relative reminders never fire on an all-day task, so all-day events instead get an absolute reminder at `todoist_all_day_reminder_time_24h`, or none if that is empty. Reminders require Todoist Pro. If your Todoist "automatic reminders" setting is on, Todoist attaches its own reminder to every task it creates with a due time; cakesync accounts for that offset instead of adding a duplicate. When your configured offsets differ from Todoist's setting, Todoist's reminder stays attached alongside cakesync's and is not removed.

## How syncing works

Every task cakesync owns carries a marker in its **description**, below a Markdown separator:

```
your own notes, never touched
---
cakesync:cal:a3f9c21e4b77#20260810T140000Z:b1946ac9 (Personal)
Room B, 3rd floor
Sprint review. Bring the demo build.
```

- Birthdays keep the original `cakesync:<UID>` form. No migration needed.
- Calendar markers hash the event UID down to 12 characters, because calendar UIDs routinely exceed 100 characters and get folded across lines by the server. The `#…` part identifies the occurrence, the `(Name)` comment is for humans and is ignored when matching.
- The trailing `:b1946ac9` fingerprints the synced body. Todoist rewrites descriptions as it stores them (a bare URL comes back as a `[label](url)` markdown link), so comparing text would rewrite the task on every run forever. The fingerprint is computed from the source instead.

**Everything above the separator is yours** and is never rewritten. For calendar tasks, everything below is refreshed whenever the source changes. Birthday descriptions are never rewritten at all.

Tasks without a `cakesync:` marker are ignored: never touched, never deleted. It is safe to keep unrelated tasks in the same project.

The marker is only read below the last `---` line, so a `cakesync:` line pasted into your own notes cannot hijack a task's identity.

Three consequences worth knowing:

- **Labels are additive.** cakesync makes sure its own label is present but never removes labels you added by hand. Changing `todoist_label` therefore leaves the old label attached; remove it yourself if you want it gone.
- **Renaming a calendar on the server invalidates its markers**, because the calendar name is part of the event hash. The next run deletes the old tasks and recreates them, losing any notes you kept above the separator on those tasks.
- **Floating events** (no timezone in the source) are resolved against the machine's local timezone, so running cakesync from hosts in different timezones will churn those specific tasks.

Reconciliation runs per Todoist project rather than per source, since several calendars can feed one project. A completed event task is not resurrected: cakesync checks completed tasks over the window before creating anything.

### Do not sync a Todoist-published calendar

If Todoist publishes your tasks into your calendar account (Fastmail shows this as a `Todoist` calendar), do not put it in `calendars`: its events are your tasks, and syncing them back duplicates them endlessly. As a safety net, cakesync drops any event whose `UID` ends in `@todoist.com` and reports the count as `skipped_loopback`.

## Failure handling

Failures are loud and never silently swallowed:

- Configuration is validated before any network call. Unknown keys, an unresolvable project name, a reminder offset outside 0 to 1440, a malformed time: all fatal, with the valid options listed.
- Sources are isolated. One failing calendar does not stop the others.
- Deletion is scoped to the source kinds that actually ran and succeeded for a project. Running `--only calendars` never touches birthday tasks in a shared project, and a calendar that legitimately returned no events still gets its stale tasks pruned.
- **A failed fetch suppresses deletion for every project that source feeds.** Treating "fetch failed" as "no events" would wipe months of tasks. This is not hypothetical: a subscribed calendar can appear in the home-set and still answer `401` to a read.
- Exit code is `1` if any source failed, so cron and monitoring can tell. The summary line always reports `errors=N`.
- A warning fires when one calendar expands past 200 occurrences.

## CLI

```
uv run cakesync [--dry-run] [--only birthdays|calendars] [--config PATH] [--log-level LEVEL]
```

- `--dry-run` reads and diffs everything, logs what would change, writes nothing.
- `--only` runs just one of the two sources.

## Logging

Logs go to both stdout and a rotating file (`logs/sync.log` by default, 1 MiB × 5 backups). File entries are timestamped; console output stays terse.

## HTTP resilience

Every HTTP call (CardDAV, CalDAV and Todoist) goes through a shared `requests.Session` with automatic retries: 3 attempts on `429` and `5xx`, exponential backoff, honouring `Retry-After`.

## Scheduling

Run it hourly rather than daily. With `sync_retain_after_end_hours: 0`, a finished event is only removed on the next run, so the interval sets how long a past event can sit in Overdue.

Cron (Linux / macOS):

```cron
0 * * * * cd /path/to/cakesync && /usr/local/bin/uv run cakesync >/dev/null 2>&1
```

Windows Task Scheduler:

```powershell
schtasks /create /tn "cakesync" /tr "uv run --directory C:\path\to\cakesync cakesync" /sc hourly
```

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format .
uv run ty check
uv run python -m pytest
```

CI (`.github/workflows/ci.yml`) runs the same commands on every push and PR.

## What it does NOT do

- It never writes to the address book or the calendars. Read-only credentials are fine and recommended.
- It does not touch tasks without a `cakesync:` marker.
- It does not compute ages — the birth year is simply appended to the task name.
- It does not sync contact fields other than the birthday, and no calendar data beyond title, time, location and description.

## License

[MIT](LICENSE)
