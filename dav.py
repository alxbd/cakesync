"""Shared WebDAV plumbing for the CardDAV and CalDAV sources."""

from __future__ import annotations

from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:carddav",
    "cal": "urn:ietf:params:xml:ns:caldav",
}

XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}


def build_session(username: str | None = None, password: str | None = None) -> requests.Session:
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
    if username is not None and password is not None:
        session.auth = HTTPBasicAuth(username, password)
    return session


def absolute(base_url: str, href: str) -> str:
    """Resolve a DAV href (usually path-only) against the server root."""
    if href.startswith(("http://", "https://")):
        return href
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}{href}"


def propfind(session: requests.Session, url: str, body: str, depth: str = "0") -> ET.Element:
    r = session.request(
        "PROPFIND",
        url,
        data=body.encode("utf-8"),
        headers={"Depth": depth, **XML_HEADERS},
        timeout=60,
    )
    r.raise_for_status()
    return ET.fromstring(r.content)


def report(session: requests.Session, url: str, body: str, depth: str = "1") -> ET.Element:
    r = session.request(
        "REPORT",
        url,
        data=body.encode("utf-8"),
        headers={"Depth": depth, **XML_HEADERS},
        timeout=120,
    )
    r.raise_for_status()
    return ET.fromstring(r.content)


def discover_home_set(session: requests.Session, root_url: str, home_prop: str) -> str:
    """Walk current-user-principal then `home_prop` (addressbook- or calendar-home-set).

    `home_prop` is a namespaced tag such as "c:addressbook-home-set".
    """
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:">'
        "<d:prop><d:current-user-principal/></d:prop>"
        "</d:propfind>"
    )
    tree = propfind(session, root_url, body)
    node = tree.find(".//d:current-user-principal/d:href", NS)
    if node is None or not node.text:
        raise RuntimeError(f"DAV: could not discover current-user-principal at {root_url}")
    principal_url = absolute(root_url, node.text)

    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"'
        ' xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        f"<d:prop><{home_prop}/></d:prop>"
        "</d:propfind>"
    )
    tree = propfind(session, principal_url, body)
    node = tree.find(f".//{home_prop}/d:href", NS)
    if node is None or not node.text:
        raise RuntimeError(f"DAV: could not discover {home_prop} for {principal_url}")
    return absolute(root_url, node.text)
