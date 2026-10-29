"""Tests for marker formatting, parsing and the user-notes contract."""

import markers


def test_calendar_marker_round_trip():
    marker = markers.calendar_marker("Personal", "abc-123@example.com", "20260810T140000Z", [])
    assert markers.find_calendar_key(marker) == markers.calendar_key(
        "Personal", "abc-123@example.com", "20260810T140000Z"
    )


def test_calendar_key_is_scoped_to_the_calendar():
    a = markers.calendar_key("Personal", "same-uid", "20260810")
    b = markers.calendar_key("Doudouz", "same-uid", "20260810")
    assert a != b


def test_long_uid_is_hashed_short():
    long_uid = "3838679i-040000008200E00074C5B7101A82E008" * 3
    marker = markers.calendar_marker("Personal", long_uid, "20260810", [])
    assert len(marker.split("#")[0]) == len("cakesync:cal:") + 12


def test_readable_comment_and_fingerprint_are_ignored_when_matching():
    with_comment = "cakesync:cal:a3f9c21e4b77#20260810:b1946ac9 (Okoone - Personal)"
    without = "cakesync:cal:a3f9c21e4b77#20260810"
    assert markers.find_calendar_key(with_comment) == markers.find_calendar_key(without)


def test_fingerprint_tracks_the_source_body_not_the_stored_text():
    # Todoist turns a bare URL into a markdown link when it stores a description,
    # so the fingerprint must come from the source body only.
    marker = markers.calendar_marker("Doudouz", "uid-1", "20260813", ["https://example.com/x"])
    stored = f"---\n{marker}\n[example](https://example.com/x)"
    assert markers.find_body_fingerprint(stored) == markers.body_fingerprint(
        ["https://example.com/x"]
    )


def test_missing_fingerprint_reads_as_none():
    assert markers.find_body_fingerprint("---\ncakesync:cal:a3f9c21e4b77#20260810") is None


def test_birthday_marker_is_not_read_as_calendar():
    description = "---\ncakesync:uid-42"
    assert markers.find_calendar_key(description) is None
    assert markers.find_birthday_uid(description) == "uid-42"


def test_calendar_marker_is_not_read_as_birthday():
    description = "---\ncakesync:cal:a3f9c21e4b77#20260810 (Personal)"
    assert markers.find_birthday_uid(description) is None


def test_user_notes_are_kept_above_the_separator():
    description = "buy a gift\nask about the dog\n---\ncakesync:cal:a3f9c21e4b77#20260810\nRoom B"
    assert markers.user_notes(description) == "buy a gift\nask about the dog"


def test_user_notes_empty_when_task_is_all_ours():
    description = "---\ncakesync:cal:a3f9c21e4b77#20260810\nRoom B"
    assert markers.user_notes(description) == ""


def test_build_description_round_trips_through_user_notes():
    built = markers.build_description(
        "my note", "cakesync:cal:a3f9c21e4b77#20260810 (Personal)", ["Room B", "Agenda here"]
    )
    assert markers.user_notes(built) == "my note"
    assert built.splitlines()[-1] == "Agenda here"


def test_build_description_skips_blank_body_lines():
    built = markers.build_description("", "cakesync:uid-1", ["", "   ", "kept"])
    assert built == "---\ncakesync:uid-1\nkept"


def test_a_pasted_marker_in_user_notes_cannot_hijack_identity():
    # Regression: the parser used to search the whole description and take the
    # first match, so a cakesync line pasted into the user's own notes stole the
    # task's identity and truncated everything below it.
    description = "line1\ncakesync:pasted-thing\nline3\n---\ncakesync:uid-alice"
    assert markers.find_birthday_uid(description) == "uid-alice"
    assert markers.user_notes(description) == "line1\ncakesync:pasted-thing\nline3"


def test_a_pasted_calendar_marker_cannot_hijack_identity():
    real = markers.calendar_marker("Personal", "uid-1", "20260810", [])
    description = f"cakesync:cal:aaaaaaaaaaaa#20260101\nmy notes\n---\n{real}"
    assert markers.find_calendar_key(description) == markers.calendar_key(
        "Personal", "uid-1", "20260810"
    )
    assert markers.user_notes(description) == "cakesync:cal:aaaaaaaaaaaa#20260101\nmy notes"


def test_legacy_marker_without_a_separator_still_matches():
    assert markers.find_birthday_uid("cakesync:uid-legacy") == "uid-legacy"


def test_separator_inside_user_text_is_not_mistaken_for_ours():
    description = "a\n---\nb\n---\ncakesync:uid-1"
    assert markers.find_birthday_uid(description) == "uid-1"
    assert markers.user_notes(description) == "a\n---\nb"
