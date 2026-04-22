"""cakesync — sync CardDAV contact birthdays to Todoist.

Works with any CardDAV-compliant server (Fastmail, iCloud, Nextcloud, Radicale,
Baïkal, SOGo…). Stateless: every task carries its source contact UID inside the
description (`cakesync:<UID>` on its own line), so the script can rebuild the
mapping at each run and decide what to create, update or delete.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
import vobject
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

log = logging.getLogger("sync")


BIRTHDAY_EMOJI = "\U0001f382"  # 🎂
# `cakesync:<uid>` must sit on its own line anywhere in the description.
UID_MARKER_RE = re.compile(r"^cakesync:\s*(\S+)\s*$", re.MULTILINE)
DAV_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}
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
# Apple Contacts.app encodes "no year" as 1604-MM-DD.
NO_YEAR_SENTINELS = {1604}
TODOIST_API = "https://api.todoist.com/api/v1"


@dataclass(frozen=True)
class Birthday:
    month: int
    day: int
    year: int | None


@dataclass(frozen=True)
class Contact:
    uid: str
    name: str
    birthday: Birthday


# ---------------------------------------------------------------------------
# HTTP session with retries
# ---------------------------------------------------------------------------


def _build_session() -> requests.Session:
    """requests.Session that retries 3 times on 429/5xx and connection errors."""
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "DELETE", "PROPFIND", "REPORT"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# CardDAV
# ---------------------------------------------------------------------------


def _propfind(session: requests.Session, url: str, body: str, depth: str = "0") -> ET.Element:
    r = session.request(
        "PROPFIND",
        url,
        data=body.encode("utf-8"),
        headers={
            "Depth": depth,
            "Content-Type": "application/xml; charset=utf-8",
        },
    )
    r.raise_for_status()
    return ET.fromstring(r.content)


def _absolute(base_url: str, href: str) -> str:
    """Resolve a DAV href (usually path-only) against the server root."""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}{href}"


def _discover_addressbooks(session: requests.Session, root_url: str) -> list[str]:
    # 1. current-user-principal
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:">'
        "<d:prop><d:current-user-principal/></d:prop>"
        "</d:propfind>"
    )
    tree = _propfind(session, root_url, body, depth="0")
    node = tree.find(".//d:current-user-principal/d:href", DAV_NS)
    if node is None or not node.text:
        raise RuntimeError("CardDAV: could not discover current-user-principal")
    principal_url = _absolute(root_url, node.text)

    # 2. addressbook-home-set
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><c:addressbook-home-set/></d:prop>"
        "</d:propfind>"
    )
    tree = _propfind(session, principal_url, body, depth="0")
    node = tree.find(".//c:addressbook-home-set/d:href", DAV_NS)
    if node is None or not node.text:
        raise RuntimeError("CardDAV: could not discover addressbook-home-set")
    home_url = _absolute(root_url, node.text)

    # 3. list addressbooks under the home-set
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:resourcetype/></d:prop>"
        "</d:propfind>"
    )
    tree = _propfind(session, home_url, body, depth="1")
    books: list[str] = []
    for response in tree.findall(".//d:response", DAV_NS):
        if response.find(".//d:resourcetype/c:addressbook", DAV_NS) is None:
            continue
        href = response.find("d:href", DAV_NS)
        if href is not None and href.text:
            books.append(_absolute(root_url, href.text))
    if not books:
        raise RuntimeError("CardDAV: no addressbooks found in home-set")
    return books


def _fetch_vcards(session: requests.Session, book_url: str) -> list[str]:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:getetag/><c:address-data/></d:prop>"
        '<c:filter><c:prop-filter name="FN"/></c:filter>'
        "</c:addressbook-query>"
    )
    r = session.request(
        "REPORT",
        book_url,
        data=body.encode("utf-8"),
        headers={
            "Depth": "1",
            "Content-Type": "application/xml; charset=utf-8",
        },
    )
    r.raise_for_status()
    tree = ET.fromstring(r.content)
    out: list[str] = []
    for addr in tree.findall(".//c:address-data", DAV_NS):
        if addr.text:
            out.append(addr.text)
    return out


def _parse_birthday(raw: str) -> Birthday | None:
    """Parse a vCard BDAY value. Accepts formats with or without year."""
    value = raw.strip()
    if not value:
        return None
    # Strip a possible time component.
    if "T" in value:
        value = value.split("T", 1)[0]
    # --MM-DD or --MMDD (year-less, vCard 4.0)
    if value.startswith("--"):
        rest = value[2:].replace("-", "")
        if len(rest) == 4 and rest.isdigit():
            return Birthday(month=int(rest[:2]), day=int(rest[2:]), year=None)
        return None
    # YYYY-MM-DD
    if "-" in value:
        parts = value.split("-")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            year = int(parts[0])
            if year in NO_YEAR_SENTINELS:
                year = None
            return Birthday(month=int(parts[1]), day=int(parts[2]), year=year)
        return None
    # YYYYMMDD
    if len(value) == 8 and value.isdigit():
        year = int(value[:4])
        if year in NO_YEAR_SENTINELS:
            year = None
        return Birthday(month=int(value[4:6]), day=int(value[6:8]), year=year)
    return None


def fetch_contacts(root_url: str, username: str, password: str) -> list[Contact]:
    session = _build_session()
    session.auth = HTTPBasicAuth(username, password)
    books = _discover_addressbooks(session, root_url)

    by_uid: dict[str, Contact] = {}
    for book in books:
        for vcf in _fetch_vcards(session, book):
            try:
                card = vobject.readOne(vcf)
            except Exception as e:  # malformed vCard — skip
                log.warning("could not parse a vCard: %s", e)
                continue
            bday_prop = getattr(card, "bday", None)
            if bday_prop is None or not bday_prop.value:
                continue
            bday = _parse_birthday(bday_prop.value)
            if bday is None:
                continue
            uid_prop = getattr(card, "uid", None)
            if uid_prop is None or not uid_prop.value:
                # Without a stable UID we cannot match across runs.
                continue
            uid = uid_prop.value.strip()
            fn = getattr(card, "fn", None)
            name = fn.value.strip() if fn and fn.value else "(no name)"
            contact = Contact(uid=uid, name=name, birthday=bday)
            existing = by_uid.get(uid)
            if existing is not None and existing != contact:
                log.warning(
                    "duplicate UID %s across addressbooks (kept %r, ignored %r)",
                    uid,
                    existing.name,
                    contact.name,
                )
                continue
            by_uid[uid] = contact
    return list(by_uid.values())


# ---------------------------------------------------------------------------
# Todoist
# ---------------------------------------------------------------------------


class TodoistClient:
    """Thin Todoist REST client. Mutations become no-ops when dry_run is set."""

    def __init__(self, token: str, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._session = _build_session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def _paginated_get(self, path: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            q = dict(params or {})
            if cursor:
                q["cursor"] = cursor
            r = self._session.get(f"{TODOIST_API}{path}", params=q, timeout=30)
            r.raise_for_status()
            body = r.json()
            out.extend(body.get("results") or [])
            cursor = body.get("next_cursor")
            if not cursor:
                break
        return out

    def list_projects(self) -> list[dict]:
        return self._paginated_get("/projects")

    def list_tasks(self, project_id: str) -> list[dict]:
        return self._paginated_get("/tasks", {"project_id": project_id})

    def create_task(self, project_id: str, content: str, description: str, due_string: str) -> None:
        if self.dry_run:
            return
        r = self._session.post(
            f"{TODOIST_API}/tasks",
            json={
                "content": content,
                "description": description,
                "project_id": project_id,
                "due_string": due_string,
                "due_lang": "en",
            },
            timeout=30,
        )
        r.raise_for_status()

    def update_task(
        self,
        task_id: str,
        content: str,
        due_string: str,
        description: str | None = None,
    ) -> None:
        if self.dry_run:
            return
        payload: dict[str, object] = {
            "content": content,
            "due_string": due_string,
            "due_lang": "en",
        }
        if description is not None:
            payload["description"] = description
        r = self._session.post(
            f"{TODOIST_API}/tasks/{task_id}",
            json=payload,
            timeout=30,
        )
        r.raise_for_status()

    def delete_task(self, task_id: str) -> None:
        if self.dry_run:
            return
        r = self._session.delete(f"{TODOIST_API}/tasks/{task_id}", timeout=30)
        r.raise_for_status()


def resolve_project_id(client: TodoistClient, project_name: str) -> str:
    wanted = project_name.strip().casefold()
    projects = client.list_projects()
    matches = [p for p in projects if (p.get("name") or "").strip().casefold() == wanted]
    if not matches:
        names = ", ".join(repr(p.get("name")) for p in projects)
        log.error("no Todoist project named %r. Available: %s", project_name, names)
        sys.exit(1)
    if len(matches) > 1:
        ids = ", ".join(str(p.get("id")) for p in matches)
        log.error("multiple Todoist projects named %r (ids: %s).", project_name, ids)
        sys.exit(1)
    return str(matches[0]["id"])


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------


def _task_content(contact: Contact) -> str:
    if contact.birthday.year is not None:
        return f"{BIRTHDAY_EMOJI} {contact.name} ({contact.birthday.year})"
    return f"{BIRTHDAY_EMOJI} {contact.name}"


def _task_due_string(bday: Birthday) -> str:
    return f"every {bday.day} {MONTHS[bday.month - 1]}"


def _task_description(contact: Contact) -> str:
    # Used only when creating a new task. On update we leave the description
    # alone so the user's own notes (added above the separator) are preserved.
    return f"---\ncakesync:{contact.uid}"


def _due_matches(existing_task: dict, bday: Birthday) -> bool:
    due = existing_task.get("due") or {}
    if not due.get("is_recurring"):
        return False
    date = due.get("date") or ""
    # "YYYY-MM-DD"
    if len(date) < 10:
        return False
    try:
        m = int(date[5:7])
        d = int(date[8:10])
    except ValueError:
        return False
    return m == bday.month and d == bday.day


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        log.error("environment variable %s is required", name)
        sys.exit(1)
    return value


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
        description="Sync contact birthdays from CardDAV into a Todoist project.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read and diff everything, but do not create/update/delete tasks",
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

    carddav_user = _require_env("CARDDAV_USERNAME")
    carddav_pass = _require_env("CARDDAV_PASSWORD")
    todoist_token = _require_env("TODOIST_API_TOKEN")
    project_name = _require_env("TODOIST_PROJECT_NAME")
    carddav_url = (
        os.environ.get("CARDDAV_URL", "https://carddav.fastmail.com/dav/").rstrip("/") + "/"
    )

    client = TodoistClient(todoist_token, dry_run=args.dry_run)
    project_id = resolve_project_id(client, project_name)

    log.info("%sFetching contacts from %s ...", prefix, carddav_url)
    contacts = fetch_contacts(carddav_url, carddav_user, carddav_pass)
    contacts_by_uid = {c.uid: c for c in contacts}
    log.info("  %d contact(s) with a birthday", len(contacts_by_uid))

    log.info("%sFetching Todoist tasks in project %s ...", prefix, project_id)
    existing_tasks = client.list_tasks(project_id)
    tasks_by_uid: dict[str, dict] = {}
    untagged = 0
    for task in existing_tasks:
        desc = task.get("description") or ""
        m = UID_MARKER_RE.search(desc)
        if m:
            tasks_by_uid[m.group(1)] = task
        else:
            untagged += 1
    log.info("  %d task(s) tagged, %d untagged (ignored)", len(tasks_by_uid), untagged)

    created = updated = deleted = unchanged = 0

    for uid, contact in contacts_by_uid.items():
        content = _task_content(contact)
        due_string = _task_due_string(contact.birthday)

        existing = tasks_by_uid.get(uid)
        if existing is None:
            log.info("  %s+ create: %s", prefix, content)
            client.create_task(project_id, content, _task_description(contact), due_string)
            created += 1
            continue

        needs_update = existing.get("content") != content or not _due_matches(
            existing, contact.birthday
        )
        if needs_update:
            log.info("  %s~ update: %s", prefix, content)
            # description intentionally omitted — preserves any user notes.
            client.update_task(existing["id"], content, due_string)
            updated += 1
        else:
            unchanged += 1

    for uid, task in tasks_by_uid.items():
        if uid in contacts_by_uid:
            continue
        log.info("  %s- delete: %s", prefix, task.get("content"))
        client.delete_task(task["id"])
        deleted += 1

    log.info(
        "%sDone. created=%d updated=%d deleted=%d unchanged=%d",
        prefix,
        created,
        updated,
        deleted,
        unchanged,
    )


if __name__ == "__main__":
    main()
