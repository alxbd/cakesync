"""CalDAV source: calendars and the individual event occurrences inside a window."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import recurring_ical_events
import requests
from icalendar import Calendar

from dav import NS, absolute, discover_home_set, propfind, report

log = logging.getLogger("sync.caldav")

# Todoist can publish your tasks back into a calendar (Fastmail shows this as a
# "Todoist" calendar). Syncing such a calendar would recreate those tasks as new
# tasks, which would be republished, and so on. Events originating from a task
# manager are recognisable by their UID domain, so we drop them before expansion.
# This is a safety net, not a licence: the real fix is not listing that calendar
# in cakesync.yml at all.
LOOPBACK_UID_DOMAINS = ("todoist.com",)


class CalendarFetchError(Exception):
    """A single calendar could not be read. Never fatal for the whole run."""


@dataclass(frozen=True)
class Occurrence:
    calendar_name: str
    uid: str
    summary: str
    location: str
    description: str
    start: datetime | date
    end: datetime | date | None
    all_day: bool

    @property
    def occurrence_id(self) -> str:
        """The `#...` half of the marker: identifies which occurrence this is."""
        if self.all_day or not isinstance(self.start, datetime):
            return self.start.strftime("%Y%m%d")
        return self.start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    @property
    def start_utc_iso(self) -> str:
        """RFC3339 UTC instant, e.g. 2026-08-10T12:00:00Z.

        The event keeps the timezone it was created in: converting to a real
        instant is what makes a 14:00 Paris meeting show as 14:00 in Paris and
        20:00 in Bangkok, rather than floating like a birthday does.
        """
        if not isinstance(self.start, datetime):
            raise ValueError("start_utc_iso is only meaningful for a timed occurrence")
        return self.start.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_calendars(session: requests.Session, root_url: str) -> dict[str, str]:
    """Map display name -> collection URL, for collections that hold events.

    VTODO-only collections (Fastmail exposes a DEFAULT_TASK_CALENDAR_NAME one)
    are skipped: they hold tasks, not events.
    """
    home_url = discover_home_set(session, root_url, "cal:calendar-home-set")
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:resourcetype/><d:displayname/>"
        "<cal:supported-calendar-component-set/></d:prop>"
        "</d:propfind>"
    )
    tree = propfind(session, home_url, body, depth="1")

    found: dict[str, str] = {}
    for response in tree.findall(".//d:response", NS):
        if response.find(".//d:resourcetype/cal:calendar", NS) is None:
            continue
        comps = [
            c.get("name")
            for c in response.findall(".//cal:supported-calendar-component-set/cal:comp", NS)
        ]
        if comps and "VEVENT" not in comps:
            continue
        href = response.find("d:href", NS)
        name = response.find(".//d:displayname", NS)
        if href is None or not href.text or name is None or not name.text:
            continue
        if name.text in found:
            # Nothing stops a server from exposing two calendars with the same
            # display name, but the config selects by name, so one is silently
            # unreachable. Say so instead of picking arbitrarily in silence.
            log.warning(
                "two calendars share the display name %r; only one can be synced", name.text
            )
        found[name.text] = absolute(root_url, href.text)
    return found


# ---------------------------------------------------------------------------
# Fetch and expand
# ---------------------------------------------------------------------------


def _calendar_query(start: datetime, end: datetime) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<cal:calendar-query xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><cal:calendar-data/></d:prop>"
        '<cal:filter><cal:comp-filter name="VCALENDAR">'
        '<cal:comp-filter name="VEVENT">'
        f'<cal:time-range start="{start.strftime("%Y%m%dT%H%M%SZ")}"'
        f' end="{end.strftime("%Y%m%dT%H%M%SZ")}"/>'
        "</cal:comp-filter></cal:comp-filter></cal:filter>"
        "</cal:calendar-query>"
    )


def _text(component, key: str) -> str:
    value = component.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _is_loopback(uid: str) -> bool:
    lowered = uid.casefold()
    return any(lowered.endswith("@" + domain) for domain in LOOPBACK_UID_DOMAINS)


def _event_end(component, start: datetime | date, all_day: bool) -> datetime | date | None:
    """Resolve the end from DTEND or DURATION. A DATE-valued DTEND is exclusive.

    The end is normalised to the same kind as the start. Broken exporters do
    emit a DATE start with a DATE-TIME end (and the reverse), and subtracting
    one from the other raises TypeError, which would abort the whole run over a
    single malformed event.
    """
    dtend = component.get("DTEND")
    if dtend is not None:
        return _match_kind(dtend.dt, all_day)
    duration = component.get("DURATION")
    if duration is not None:
        return _match_kind(start + duration.dt, all_day)
    if all_day:
        return start + timedelta(days=1)
    return None


def _match_kind(value: datetime | date, all_day: bool) -> datetime | date | None:
    if all_day:
        return value.date() if isinstance(value, datetime) else value
    # A timed event with a date-only end tells us nothing usable: drop it
    # rather than guess a time, and the task simply gets no duration.
    return value if isinstance(value, datetime) else None


def fetch_occurrences(
    session: requests.Session,
    calendar_url: str,
    calendar_name: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[Occurrence], int]:
    """Expand a calendar into individual occurrences inside the window.

    Returns the occurrences plus the number of source objects skipped by the
    loopback guard. Raises CalendarFetchError if the calendar cannot be read,
    which happens for real: a subscribed calendar can be listed in the home-set
    and still answer 401 to a REPORT.
    """
    try:
        tree = report(session, calendar_url, _calendar_query(window_start, window_end))
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        raise CalendarFetchError(f"HTTP {status} reading calendar {calendar_name!r}") from e
    except requests.RequestException as e:
        raise CalendarFetchError(f"{type(e).__name__} reading calendar {calendar_name!r}") from e

    occurrences: list[Occurrence] = []
    skipped_loopback = 0

    for data in tree.findall(".//cal:calendar-data", NS):
        if not data.text:
            continue
        try:
            calendar = Calendar.from_ical(data.text)
        except Exception as e:  # malformed iCalendar — skip this object, not the run
            log.warning("%s: could not parse a calendar object: %s", calendar_name, e)
            continue

        if any(_is_loopback(str(c.get("UID") or "")) for c in calendar.walk("VEVENT")):
            skipped_loopback += 1
            continue

        try:
            events = recurring_ical_events.of(calendar).between(window_start, window_end)
        except Exception as e:
            log.warning("%s: could not expand a recurring event: %s", calendar_name, e)
            continue

        for component in events:
            if _text(component, "STATUS").upper() == "CANCELLED":
                continue
            uid = _text(component, "UID")
            if not uid:
                continue  # no stable identity, cannot match across runs
            dtstart = component.get("DTSTART")
            if dtstart is None:
                continue
            start = dtstart.dt
            all_day = isinstance(start, date) and not isinstance(start, datetime)
            if isinstance(start, datetime) and start.tzinfo is None:
                # Floating event: interpret in the machine's local timezone.
                start = start.astimezone()
            occurrences.append(
                Occurrence(
                    calendar_name=calendar_name,
                    uid=uid,
                    summary=_text(component, "SUMMARY") or "(no title)",
                    location=_text(component, "LOCATION"),
                    description=_text(component, "DESCRIPTION"),
                    start=start,
                    end=_event_end(component, start, all_day),
                    all_day=all_day,
                )
            )

    return occurrences, skipped_loopback
