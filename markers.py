"""Task identity markers.

Every task cakesync owns carries a marker line in its description, below a `---`
separator. The mapping between source objects and Todoist tasks is rebuilt from
these on every run, which is what makes cakesync stateless.

    cakesync:<vCard UID>                                             birthday (legacy, unchanged)
    cakesync:cal:a3f9c21e4b77#20260810T140000Z:b1946ac9 (Personal)   timed occurrence
    cakesync:cal:a3f9c21e4b77#20260810:b1946ac9 (Personal)           all-day occurrence

Calendar UIDs routinely run past 100 characters and get folded across lines by
the server, so they are hashed down to 12 hex chars. The calendar name in
parentheses is purely for humans reading the task; it is ignored when matching.

The trailing `:b1946ac9` fingerprints the synced body (location and event
description). Todoist rewrites descriptions as it stores them — a bare URL comes
back as a `[label](url)` markdown link — so comparing the text we sent against
the text Todoist kept would report a difference on every single run and rewrite
the task forever. The fingerprint is computed from the source, not from what
Todoist rendered, so it only changes when the calendar actually changes.
"""

from __future__ import annotations

import hashlib
import re

SEPARATOR = "---"

CAL_MARKER_RE = re.compile(
    r"^cakesync:cal:([0-9a-f]{12}#[0-9A-Z]+)(?::([0-9a-f]{8}))?(?:\s+\(.*\))?\s*$",
    re.MULTILINE,
)
# A birthday marker is anything after `cakesync:` that is not the `cal:` namespace.
BDAY_MARKER_RE = re.compile(r"^cakesync:(?!cal:)(\S+)\s*$", re.MULTILINE)


def event_key(calendar_name: str, uid: str) -> str:
    """Stable short id for an event within a calendar."""
    digest = hashlib.sha1(f"{calendar_name}\x00{uid}".encode()).hexdigest()
    return digest[:12]


def body_fingerprint(body: list[str]) -> str:
    """Short digest of the synced description body, as it came from the source."""
    joined = "\n".join(line for line in body if line and line.strip())
    return hashlib.sha1(joined.encode()).hexdigest()[:8]


def calendar_marker(calendar_name: str, uid: str, occurrence: str, body: list[str]) -> str:
    key = calendar_key(calendar_name, uid, occurrence)
    return f"cakesync:cal:{key}:{body_fingerprint(body)} ({calendar_name})"


def calendar_key(calendar_name: str, uid: str, occurrence: str) -> str:
    """The part of the marker used for matching, without the readable comment."""
    return f"{event_key(calendar_name, uid)}#{occurrence}"


def birthday_marker(uid: str) -> str:
    return f"cakesync:{uid}"


def _locate(description: str) -> tuple[int | None, int | None]:
    """Find our block: `(separator_line, marker_line)`, either may be None.

    The marker is only looked for *below* the last separator. Searching the
    whole description would let a `cakesync:` line the user pasted into their
    own notes hijack the task's identity — the task would then be matched to
    the wrong source, or to none, and deleted as an orphan.

    The separator-less fallback exists for tasks written before the separator
    convention, and only applies when the description has no separator at all,
    so it cannot be triggered by user text sitting above a real marker.
    """
    lines = (description or "").splitlines()
    separators = [i for i, line in enumerate(lines) if line.strip() == SEPARATOR]

    for sep in reversed(separators):
        for i in range(sep + 1, len(lines)):
            if CAL_MARKER_RE.match(lines[i]) or BDAY_MARKER_RE.match(lines[i]):
                return sep, i

    if not separators:
        for i, line in enumerate(lines):
            if CAL_MARKER_RE.match(line) or BDAY_MARKER_RE.match(line):
                return None, i
    return None, None


def _marker_line(description: str) -> str | None:
    _, marker_at = _locate(description)
    if marker_at is None:
        return None
    return (description or "").splitlines()[marker_at]


def find_calendar_key(description: str) -> str | None:
    m = CAL_MARKER_RE.match(_marker_line(description) or "")
    return m.group(1) if m else None


def find_body_fingerprint(description: str) -> str | None:
    """The fingerprint stored in the marker, or None for a pre-fingerprint task."""
    m = CAL_MARKER_RE.match(_marker_line(description) or "")
    return m.group(2) if m else None


def find_birthday_uid(description: str) -> str | None:
    m = BDAY_MARKER_RE.match(_marker_line(description) or "")
    return m.group(1) if m else None


def user_notes(description: str) -> str:
    """Everything the user wrote above the separator that precedes our marker.

    Anything below is ours and gets rewritten on update; anything above is
    theirs and is never touched.
    """
    if not description:
        return ""
    lines = description.splitlines()
    sep_at, marker_at = _locate(description)
    if marker_at is None:
        return description.rstrip()
    cut = sep_at if sep_at is not None else marker_at
    return "\n".join(lines[:cut]).rstrip()


def build_description(notes: str, marker: str, body: list[str]) -> str:
    """Assemble `notes` + separator + marker + our synced body lines."""
    parts: list[str] = []
    if notes.strip():
        parts.append(notes.rstrip())
    parts.append(SEPARATOR)
    parts.append(marker)
    parts.extend(line for line in body if line and line.strip())
    return "\n".join(parts)
