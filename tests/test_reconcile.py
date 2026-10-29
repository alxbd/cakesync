"""Tests for the reconcile loop, the only code that deletes anything."""

from datetime import UTC, datetime

import markers
from caldav import Occurrence
from carddav import Birthday, Contact
from config import BirthdayConfig, CalendarConfig
from sync import Stats, birthday_tasks, occurrence_tasks, reconcile_project

# Before every occurrence used here, so none of them has started yet.
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class FakeClient:
    """Records mutations instead of performing them."""

    def __init__(self, tasks=None, completed=None):
        self._tasks = tasks or []
        self._completed = completed or []
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.reminders: list[tuple] = []

    def list_tasks(self, project_id):
        return self._tasks

    def list_completed_tasks(self, project_id):
        return self._completed

    def list_reminders(self):
        return []

    def create_task(self, payload):
        self.created.append(payload)
        return f"new-{len(self.created)}"

    def update_task(self, task_id, payload):
        self.updated.append((task_id, payload))

    def delete_task(self, task_id):
        self.deleted.append(task_id)

    def add_relative_reminder(self, task_id, minute_offset):
        self.reminders.append(("relative", task_id, minute_offset))

    def add_absolute_reminder(self, task_id, due_datetime):
        self.reminders.append(("absolute", task_id, due_datetime))


def birthday_task_row(uid, task_id="1", content="🎂 Alice"):
    return {
        "id": task_id,
        "content": content,
        "description": f"---\n{markers.birthday_marker(uid)}",
        "due": {"date": "2026-01-15", "is_recurring": True},
        "labels": [],
        "duration": None,
    }


def calendar_occurrence(summary="Standup", uid="uid-1"):
    return Occurrence(
        calendar_name="Personal",
        uid=uid,
        summary=summary,
        location="",
        description="",
        start=datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
        end=None,
        all_day=False,
    )


def run(client, desired, synced_kinds, allow_delete=True):
    stats = Stats()
    reconcile_project(
        client,
        "p1",
        "Agenda",
        desired,
        synced_kinds=synced_kinds,
        allow_delete=allow_delete,
        dry_run=False,
        stats=stats,
    )
    return stats


# -- the delete path --------------------------------------------------------


def test_only_calendars_does_not_delete_birthday_tasks():
    """Regression: a shared project lost every birthday task under --only calendars."""
    client = FakeClient(tasks=[birthday_task_row("uid-alice")])
    cal = CalendarConfig(caldav_calendar_name="Personal", todoist_project_name="Agenda")
    desired = occurrence_tasks(cal, "p1", [calendar_occurrence()], NOW)

    run(client, desired, synced_kinds={"calendar"})

    assert client.deleted == []
    assert len(client.created) == 1


def test_calendar_that_emptied_is_pruned():
    """A calendar that ran and returned nothing must still clean up its tasks."""
    cal = CalendarConfig(caldav_calendar_name="Personal", todoist_project_name="Agenda")
    stale = occurrence_tasks(cal, "p1", [calendar_occurrence()], NOW)[0]
    existing = {
        "id": "9",
        "content": stale.content,
        "description": markers.build_description("", stale.marker, stale.body),
        "due": {},
        "labels": [],
        "duration": None,
    }

    client = FakeClient(tasks=[existing])
    run(client, [], synced_kinds={"calendar"})

    assert client.deleted == ["9"]


def test_nothing_is_deleted_when_the_source_failed():
    client = FakeClient(tasks=[birthday_task_row("uid-gone")])
    run(client, [], synced_kinds={"birthday"}, allow_delete=False)
    assert client.deleted == []


def test_untagged_tasks_are_never_touched():
    untagged = {
        "id": "7",
        "content": "my own task",
        "description": "nothing to do with cakesync",
        "due": {},
        "labels": [],
        "duration": None,
    }
    client = FakeClient(tasks=[untagged])
    run(client, [], synced_kinds={"birthday", "calendar"})
    assert client.deleted == []
    assert client.updated == []


def test_stale_birthday_task_is_deleted_when_birthdays_ran():
    client = FakeClient(tasks=[birthday_task_row("uid-gone", task_id="4")])
    run(client, [], synced_kinds={"birthday"})
    assert client.deleted == ["4"]


# -- resurrection guard -----------------------------------------------------


def test_completed_occurrence_is_not_recreated():
    cal = CalendarConfig(caldav_calendar_name="Personal", todoist_project_name="Agenda")
    desired = occurrence_tasks(cal, "p1", [calendar_occurrence()], NOW)
    completed = [{"description": markers.build_description("", desired[0].marker, [])}]

    client = FakeClient(completed=completed)
    stats = run(client, desired, synced_kinds={"calendar"})

    assert client.created == []
    assert stats.skipped_completed == 1


# -- idempotency ------------------------------------------------------------


def _bday_desired():
    cfg = BirthdayConfig(todoist_project_name="Birthdays", todoist_task_emoji="🎂")
    contact = Contact(uid="uid-alice", name="Alice", birthday=Birthday(month=1, day=15, year=None))
    return birthday_tasks(cfg, "p1", [contact])


def test_matching_birthday_task_is_left_alone():
    desired = _bday_desired()
    row = birthday_task_row("uid-alice", content=desired[0].content)
    client = FakeClient(tasks=[row])

    stats = run(client, desired, synced_kinds={"birthday"})

    assert client.updated == []
    assert stats.unchanged == 1


def test_user_added_label_survives_an_update():
    cal = CalendarConfig(
        caldav_calendar_name="Personal", todoist_project_name="Agenda", todoist_label="Personal"
    )
    desired = occurrence_tasks(cal, "p1", [calendar_occurrence()], NOW)[0]
    existing = {
        "id": "3",
        "content": "stale title",  # forces an update
        "description": markers.build_description("", desired.marker, desired.body),
        "due": desired.due and {"date": "2026-08-10T14:00:00Z"},
        "labels": ["Personal", "MyOwnLabel"],
        "duration": None,
    }

    client = FakeClient(tasks=[existing])
    run(client, [desired], synced_kinds={"calendar"})

    assert client.updated, "a changed title should trigger an update"
    payload = client.updated[0][1]
    # Either labels are left out of the payload entirely (nothing to change) or
    # they are sent merged. What must never happen is the user's label vanishing.
    assert "MyOwnLabel" in payload.get("labels", ["MyOwnLabel"])


def test_user_notes_survive_a_calendar_update():
    """The event body changed, so we rewrite below the separator but not above."""
    cal = CalendarConfig(caldav_calendar_name="Personal", todoist_project_name="Agenda")
    occ = calendar_occurrence()
    desired = occurrence_tasks(
        cal,
        "p1",
        [
            Occurrence(
                calendar_name=occ.calendar_name,
                uid=occ.uid,
                summary=occ.summary,
                location="Room B",  # new location -> new fingerprint
                description="",
                start=occ.start,
                end=occ.end,
                all_day=False,
            )
        ],
        NOW,
    )[0]
    stale_marker = markers.calendar_marker("Personal", occ.uid, occ.occurrence_id, [])
    existing = {
        "id": "5",
        "content": desired.content,
        "description": "buy a gift\n---\n" + stale_marker,
        "due": {"date": "2026-08-10T14:00:00Z"},
        "labels": [],
        "duration": None,
    }

    client = FakeClient(tasks=[existing])
    run(client, [desired], synced_kinds={"calendar"})

    assert client.updated, "a changed event body should trigger an update"
    description = client.updated[0][1]["description"]
    assert description.startswith("buy a gift")
    assert "Room B" in description


def test_unchanged_body_does_not_rewrite_the_description():
    """The infinite-update-loop regression: same source body, no description write."""
    cal = CalendarConfig(caldav_calendar_name="Personal", todoist_project_name="Agenda")
    desired = occurrence_tasks(cal, "p1", [calendar_occurrence()], NOW)[0]
    existing = {
        "id": "6",
        "content": "stale title",  # forces an update for some other reason
        "description": markers.build_description("", desired.marker, desired.body),
        "due": {"date": "2026-08-10T14:00:00Z"},
        "labels": [],
        "duration": None,
    }

    client = FakeClient(tasks=[existing])
    run(client, [desired], synced_kinds={"calendar"})

    assert "description" not in client.updated[0][1]
