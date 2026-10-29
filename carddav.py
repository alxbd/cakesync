"""CardDAV source: contacts and their birthdays."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
import vobject

from dav import NS, absolute, build_session, discover_home_set, propfind, report

log = logging.getLogger("sync.carddav")

# Apple Contacts.app encodes "no year" as 1604-MM-DD.
NO_YEAR_SENTINELS = {1604}


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


def parse_birthday(raw: str) -> Birthday | None:
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


def _discover_addressbooks(session: requests.Session, root_url: str) -> list[str]:
    home_url = discover_home_set(session, root_url, "c:addressbook-home-set")
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        "<d:prop><d:resourcetype/></d:prop>"
        "</d:propfind>"
    )
    tree = propfind(session, home_url, body, depth="1")
    books: list[str] = []
    for response in tree.findall(".//d:response", NS):
        if response.find(".//d:resourcetype/c:addressbook", NS) is None:
            continue
        href = response.find("d:href", NS)
        if href is not None and href.text:
            books.append(absolute(root_url, href.text))
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
    tree = report(session, book_url, body)
    return [addr.text for addr in tree.findall(".//c:address-data", NS) if addr.text]


def fetch_contacts(root_url: str, username: str, password: str) -> list[Contact]:
    session = build_session(username, password)
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
            bday = parse_birthday(bday_prop.value)
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
