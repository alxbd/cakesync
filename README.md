# cakesync 🎂

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Ruff](https://img.shields.io/badge/linter-ruff-261230.svg)
![Type checker: ty](https://img.shields.io/badge/typecheck-ty-261230.svg)

Sync contact birthdays from any CardDAV address book into a Todoist project, as yearly recurring tasks.

Each run reads every contact that has a `BDAY` field and reconciles the Todoist project: creating missing tasks, updating changed ones, and deleting tasks whose source contact is gone. No local state file — the source of truth is the address book plus the tasks themselves.

## Compatible services

Works with any CardDAV-compliant server that accepts HTTP Basic Auth and implements the standard discovery flow (RFC 6352):

| Service | Status | Notes |
|---|---|---|
| **Fastmail** | ✅ Tested | Use an app-specific password (*Settings → Privacy & Security → Integrations*) |
| **iCloud** | ✅ Expected | App-specific password required |
| **Nextcloud** / **ownCloud** | ✅ Expected | |
| **Radicale**, **Baïkal**, **SOGo** | ✅ Expected | |
| **Synology Contacts** | ✅ Expected | |
| Google Contacts | ❌ Not supported | Google shut down CardDAV access — requires OAuth2 |

See `.env.example` for sample `CARDDAV_URL` values.

## How matching works (stateless)

Each Todoist task carries its source contact's vCard `UID` inside its **description**, tucked below a Markdown separator:

```
---
cakesync:<UID>
```

On every run the script reads those markers to pair tasks with contacts, so the mapping is rebuilt from scratch and nothing persists between runs. Tasks in the project without a `cakesync:` marker are ignored — never touched, never deleted — so it is safe to keep unrelated tasks in the same project.

**You can freely edit the description above the separator**: add notes, gift ideas, addresses. The sync only looks for the marker line and never rewrites the description on update.

## Task format

- Content: `🎂 <Full Name>` — with `(<year>)` appended when the birth year is known
- Description: `---\ncakesync:<UID>` on creation; preserved as-is on updates
- Due: `every <day> <Month>` (e.g. `every 15 January`), yearly recurring
- Birth-year placeholders commonly emitted by Apple Contacts (`1604-MM-DD`) are treated as "no year"

Supported `BDAY` formats: `YYYY-MM-DD`, `YYYYMMDD`, `--MM-DD`, `--MMDD`, and the same forms with a trailing `T…` time component.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
# edit .env — see below
uv sync
uv run cakesync
```

### Environment variables

| Variable | Purpose |
|---|---|
| `CARDDAV_USERNAME` | User name for CardDAV Basic Auth (usually the email address) |
| `CARDDAV_PASSWORD` | App-specific password for CardDAV |
| `TODOIST_API_TOKEN` | From *Settings → Integrations → Developer* in Todoist |
| `TODOIST_PROJECT_NAME` | Case-insensitive name of the Todoist project that receives birthday tasks |
| `CARDDAV_URL` | Optional. Defaults to `https://carddav.fastmail.com/dav/`. See `.env.example` for other providers. |
| `LOG_LEVEL` | Optional. Defaults to `INFO`. |
| `LOG_FILE` | Optional. Defaults to `logs/sync.log`. |
| `LOG_MAX_BYTES` | Optional. Defaults to `1048576` (1 MiB). |
| `LOG_BACKUP_COUNT` | Optional. Defaults to `5`. |

If the project name does not match (or matches several projects), the script prints the list of available projects and exits without touching anything.

## CLI

```
uv run cakesync [--dry-run] [--log-level LEVEL]
```

- `--dry-run` reads the address book and the Todoist project, computes the diff, logs what would change, but does not create, update or delete anything.
- `--log-level` overrides the `LOG_LEVEL` env for the current run.

## Logging

Logs go to both stdout and a rotating file (`logs/sync.log` by default, 1 MiB × 5 backups). File entries are timestamped; console output stays terse.

## HTTP resilience

Every HTTP call (CardDAV and Todoist) goes through a shared `requests.Session` with automatic retries: 3 attempts on `429` and `5xx` responses, with exponential backoff, honouring `Retry-After`.

## Scheduling

Cron (Linux / macOS):

```cron
0 7 * * * cd /path/to/cakesync && /usr/local/bin/uv run cakesync >/dev/null 2>&1
```

Windows Task Scheduler:

```powershell
schtasks /create /tn "cakesync" /tr "uv run --directory C:\path\to\cakesync cakesync" /sc daily /st 07:00
```

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format .
uv run ty check
uv run pytest
```

CI (`.github/workflows/ci.yml`) runs the same commands on every push and PR.

## What it does NOT do

- It does not modify the source address book.
- It does not touch tasks without a `cakesync:` marker.
- It does not compute ages — the birth year is simply appended to the task name.
- It does not sync anything other than birthdays (no phone, email, address, etc.).

## License

[MIT](LICENSE)
