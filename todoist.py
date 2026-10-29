"""Thin Todoist API v1 client. Mutations become no-ops when dry_run is set."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from dav import build_session

log = logging.getLogger("sync.todoist")

TODOIST_API = "https://api.todoist.com/api/v1"
# The completed-by-completion-date endpoint refuses windows wider than 3 months.
# 88 rather than 90 leaves headroom for stretches of long months.
COMPLETED_LOOKBACK_DAYS = 88


class TodoistClient:
    def __init__(self, token: str, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._session = build_session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})
        self._user: dict | None = None

    # -- reads ---------------------------------------------------------------

    def _paginated_get(
        self, path: str, params: dict | None = None, key: str = "results"
    ) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            q = dict(params or {})
            if cursor:
                q["cursor"] = cursor
            r = self._session.get(f"{TODOIST_API}{path}", params=q, timeout=60)
            r.raise_for_status()
            body = r.json()
            out.extend(body.get(key) or [])
            cursor = body.get("next_cursor")
            if not cursor:
                break
        return out

    def list_projects(self) -> list[dict]:
        return self._paginated_get("/projects")

    def list_labels(self) -> list[dict]:
        return self._paginated_get("/labels")

    def list_tasks(self, project_id: str) -> list[dict]:
        return self._paginated_get("/tasks", {"project_id": project_id})

    def list_completed_tasks(self, project_id: str) -> list[dict]:
        """Tasks completed in this project over the last COMPLETED_LOOKBACK_DAYS.

        Needed so a ticked-off event is not resurrected on the next run: an
        active-task listing simply does not contain it any more.
        """
        now = datetime.now(UTC)
        params = {
            "since": (now - timedelta(days=COMPLETED_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S"),
            "until": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "project_id": project_id,
        }
        return self._paginated_get("/tasks/completed/by_completion_date", params, key="items")

    def list_reminders(self) -> list[dict]:
        return self._paginated_get("/reminders")

    def _profile(self) -> dict:
        """The account's profile and settings, fetched once per run."""
        if self._user is None:
            r = self._session.get(f"{TODOIST_API}/user", timeout=30)
            r.raise_for_status()
            self._user = r.json()
        return self._user

    def account_timezone(self) -> str | None:
        """The timezone set on the Todoist account, e.g. "Asia/Phnom_Penh"."""
        return (self._profile().get("tz_info") or {}).get("timezone") or None

    def auto_reminder_offset(self) -> int | None:
        """The user's "automatic reminders" setting, in minutes before due time.

        Todoist applies this itself to every task created with a due time, so a
        task we just created may already carry a relative reminder we never
        asked for. Without accounting for it we add a duplicate on the run after
        creation, every time. None means the feature is off.
        """
        value = self._profile().get("auto_reminder")
        return value if isinstance(value, int) else None

    # -- writes --------------------------------------------------------------

    def create_label(self, name: str) -> None:
        if self.dry_run:
            return
        r = self._session.post(f"{TODOIST_API}/labels", json={"name": name}, timeout=30)
        r.raise_for_status()

    def create_task(self, payload: dict[str, Any]) -> str | None:
        """Create a task and return its id (None in dry-run)."""
        if self.dry_run:
            return None
        r = self._session.post(f"{TODOIST_API}/tasks", json=payload, timeout=30)
        r.raise_for_status()
        return str(r.json().get("id"))

    def update_task(self, task_id: str, payload: dict[str, Any]) -> None:
        if self.dry_run:
            return
        r = self._session.post(f"{TODOIST_API}/tasks/{task_id}", json=payload, timeout=30)
        r.raise_for_status()

    def delete_task(self, task_id: str) -> None:
        if self.dry_run:
            return
        r = self._session.delete(f"{TODOIST_API}/tasks/{task_id}", timeout=30)
        r.raise_for_status()

    def add_relative_reminder(self, task_id: str, minute_offset: int) -> None:
        """Reminder firing `minute_offset` minutes before the task's due time."""
        if self.dry_run:
            return
        r = self._session.post(
            f"{TODOIST_API}/reminders",
            json={
                "task_id": task_id,
                "reminder_type": "relative",
                "minute_offset": minute_offset,
            },
            timeout=30,
        )
        r.raise_for_status()

    def add_absolute_reminder(self, task_id: str, due_datetime: str) -> None:
        """Reminder at a fixed wall-clock moment, e.g. "2026-08-21T09:00:00".

        Used for all-day tasks, where a relative reminder has no due time to
        hang off and would silently never fire.
        """
        if self.dry_run:
            return
        r = self._session.post(
            f"{TODOIST_API}/reminders",
            json={
                "task_id": task_id,
                "reminder_type": "absolute",
                "due": {"date": due_datetime},
            },
            timeout=30,
        )
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def resolve_project_ids(client: TodoistClient, names: set[str]) -> dict[str, str]:
    """Map project names (case-insensitive) to ids. Exits if any is unresolvable."""
    projects = client.list_projects()
    by_name: dict[str, list[dict]] = {}
    for p in projects:
        by_name.setdefault((p.get("name") or "").strip().casefold(), []).append(p)

    resolved: dict[str, str] = {}
    for name in names:
        matches = by_name.get(name.strip().casefold(), [])
        if not matches:
            available = ", ".join(sorted(repr(p.get("name")) for p in projects))
            log.error("no Todoist project named %r. Available: %s", name, available)
            sys.exit(1)
        if len(matches) > 1:
            ids = ", ".join(str(p.get("id")) for p in matches)
            log.error("multiple Todoist projects named %r (ids: %s)", name, ids)
            sys.exit(1)
        resolved[name] = str(matches[0]["id"])
    return resolved


def ensure_labels(client: TodoistClient, names: set[str], *, prefix: str = "") -> None:
    """Create any label that does not exist yet.

    Todoist does NOT create a personal label when an unknown name is attached to
    a task: the string sticks to the task and filters still work, but the label
    never shows up in the sidebar. Verified against the live API.
    """
    wanted = {n for n in names if n}
    if not wanted:
        return
    existing = {(label.get("name") or "").casefold() for label in client.list_labels()}
    for name in sorted(wanted):
        if name.casefold() in existing:
            continue
        log.info("  %s+ label: %s", prefix, name)
        client.create_label(name)
