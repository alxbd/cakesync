"""Tests for the BDAY parsing logic."""

from main import Birthday, _parse_birthday


def test_iso_full_date():
    assert _parse_birthday("1990-01-15") == Birthday(month=1, day=15, year=1990)


def test_iso_compact():
    assert _parse_birthday("19900115") == Birthday(month=1, day=15, year=1990)


def test_yearless_with_dashes():
    assert _parse_birthday("--01-15") == Birthday(month=1, day=15, year=None)


def test_yearless_compact():
    assert _parse_birthday("--0115") == Birthday(month=1, day=15, year=None)


def test_with_time_component_is_stripped():
    assert _parse_birthday("1990-01-15T00:00:00") == Birthday(month=1, day=15, year=1990)


def test_apple_sentinel_strips_year():
    # Apple Contacts.app stores "no year" as 1604-MM-DD — treat as year-less.
    assert _parse_birthday("1604-03-22") == Birthday(month=3, day=22, year=None)


def test_apple_sentinel_compact_strips_year():
    assert _parse_birthday("16040322") == Birthday(month=3, day=22, year=None)


def test_surrounding_whitespace_is_tolerated():
    assert _parse_birthday("  1990-01-15  ") == Birthday(month=1, day=15, year=1990)


def test_empty_returns_none():
    assert _parse_birthday("") is None
    assert _parse_birthday("   ") is None


def test_garbage_returns_none():
    assert _parse_birthday("not-a-date") is None


def test_malformed_compact_returns_none():
    assert _parse_birthday("1234567") is None  # 7 digits, not 8
