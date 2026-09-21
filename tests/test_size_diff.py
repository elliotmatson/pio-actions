import json

import pytest

from scripts.size_diff import (
    MARKER,
    diff_env,
    load_manifests,
    render,
    section_deltas,
    signed,
    verdicts,
)

CEILING = 1966080


def manifest(env, bin_bytes, flash=None, ram=None, sections=None, pct=None):
    return {
        "env": env,
        "bin_bytes": bin_bytes,
        "flash_bytes": flash if flash is not None else bin_bytes - 1000,
        "ram_bytes": ram if ram is not None else 40_000,
        "app_partition_bytes": CEILING,
        "partition_pct": pct if pct is not None else round(100 * bin_bytes / CEILING, 2),
        "sections": sections or {},
    }


def section(size):
    return {"size": size, "flash": size, "ram": 0}


# --- deltas ------------------------------------------------------------------

def test_growth_is_reported_as_a_signed_delta():
    d = diff_env("esp32dev", manifest("esp32dev", 272_000), manifest("esp32dev", 271_000))
    assert d["bin_delta"] == 1000


def test_shrinking_is_reported_too():
    d = diff_env("esp32dev", manifest("esp32dev", 270_000), manifest("esp32dev", 271_000))
    assert d["bin_delta"] == -1000


def test_an_env_with_no_baseline_has_no_delta():
    d = diff_env("esp32s3", manifest("esp32s3", 300_000), None)
    assert d["bin_delta"] is None and d["sections"] == []


def test_a_manifest_missing_elf_totals_does_not_crash_the_delta():
    head = manifest("esp32dev", 272_000)
    head["flash_bytes"] = None
    d = diff_env("esp32dev", head, manifest("esp32dev", 271_000))
    assert d["flash_delta"] is None
    assert d["bin_delta"] == 1000


@pytest.mark.parametrize("value,expected", [
    (0, "0"),
    (1024, "+1.0 KB"),
    (-1024, "-1.0 KB"),
    (None, "n/a"),
])
def test_signed_formatting(value, expected):
    assert signed(value) == expected


# --- sections ----------------------------------------------------------------

def test_only_changed_sections_are_listed_biggest_first():
    base = manifest("e", 100, sections={
        ".flash.text": section(1000), ".flash.rodata": section(500), ".bss": section(20)})
    head = manifest("e", 100, sections={
        ".flash.text": section(1100), ".flash.rodata": section(500), ".bss": section(2020)})
    rows = section_deltas(head, base)
    assert [r[0] for r in rows] == [".bss", ".flash.text"]
    assert rows[0][3] == 2000


def test_a_section_added_or_removed_shows_against_zero():
    base = manifest("e", 100, sections={".old": section(64)})
    head = manifest("e", 100, sections={".new": section(128)})
    rows = dict((r[0], r[3]) for r in section_deltas(head, base))
    assert rows == {".old": -64, ".new": 128}


# --- verdicts ----------------------------------------------------------------

def test_an_image_that_cannot_flash_is_an_error():
    d = [diff_env("e", manifest("e", CEILING + 10, pct=100.5), manifest("e", 100))]
    assert "will not flash" in verdicts(d, 2048, 95.0)[0]


def test_crossing_the_partition_limit_is_an_error():
    d = [diff_env("e", manifest("e", 100, pct=96.0), manifest("e", 100, pct=94.0))]
    problems = verdicts(d, 2048, 95.0)
    assert len(problems) == 1 and "96.0%" in problems[0]


def test_a_comfortable_image_raises_nothing():
    d = [diff_env("e", manifest("e", 271_000), manifest("e", 270_000))]
    assert verdicts(d, 2048, 95.0) == []


def test_growth_alone_does_not_fail_the_job():
    # Deliberate: reviewers should see a big delta, not be blocked by it.
    d = [diff_env("e", manifest("e", 400_000), manifest("e", 271_000))]
    assert verdicts(d, 2048, 95.0) == []


def test_an_unknown_partition_ceiling_cannot_raise_a_verdict():
    head = manifest("e", 271_000)
    head["partition_pct"] = None
    assert verdicts([diff_env("e", head, None)], 2048, 95.0) == []


# --- rendering ---------------------------------------------------------------

def test_comment_carries_the_marker_so_it_can_be_updated_in_place():
    body = render([diff_env("e", manifest("e", 100), manifest("e", 100))], "abc", 2048, [])
    assert body.startswith(MARKER)


def test_an_unchanged_build_says_so_plainly():
    body = render([diff_env("e", manifest("e", 271_000), manifest("e", 271_000))],
                  "abc", 2048, [])
    assert "**No change**" in body


def test_the_partition_cell_shows_the_move():
    body = render([diff_env("e", manifest("e", 100, pct=13.9), manifest("e", 100, pct=13.8))],
                  "abc", 2048, [])
    assert "13.8% → 13.9%" in body


def test_a_large_delta_is_flagged_but_a_small_one_is_not():
    big = render([diff_env("e", manifest("e", 280_000), manifest("e", 271_000))],
                 "abc", 2048, [])
    small = render([diff_env("e", manifest("e", 271_100), manifest("e", 271_000))],
                   "abc", 2048, [])
    assert "⚠️" in big and "⚠️" not in small


def test_a_new_env_is_labelled_rather_than_shown_as_zero_change():
    body = render([diff_env("e", manifest("e", 271_000), None)], "abc", 2048, [])
    assert "*(new)*" in body and "| n/a |" in body


def test_problems_are_surfaced_in_the_comment_body():
    body = render([diff_env("e", manifest("e", 100), manifest("e", 100))],
                  "abc", 2048, ["e: image is 99% of the app partition"])
    assert "[!CAUTION]" in body and "99%" in body


def test_section_details_are_collapsed(tmp_path):
    base = manifest("e", 100, sections={".flash.text": section(1000)})
    head = manifest("e", 100, sections={".flash.text": section(9000)})
    body = render([diff_env("e", head, base)], "abc", 2048, [])
    assert "<details>" in body and "`.flash.text`" in body


# --- loading -----------------------------------------------------------------

def test_manifests_are_keyed_by_the_env_they_describe(tmp_path):
    for env in ("esp32dev", "esp32s3"):
        (tmp_path / f"{env}-manifest.json").write_text(json.dumps(manifest(env, 100)))
    assert sorted(load_manifests(str(tmp_path))) == ["esp32dev", "esp32s3"]


def test_an_empty_directory_loads_nothing(tmp_path):
    assert load_manifests(str(tmp_path)) == {}


# --- symbols -----------------------------------------------------------------

def with_symbols(env, syms, truncated=False):
    m = manifest(env, 271_000)
    m["symbols"] = syms
    m["symbols_truncated"] = truncated
    return m


def test_symbol_growth_is_attributed_to_the_symbol():
    from scripts.size_diff import symbol_deltas

    rows = symbol_deltas(
        with_symbols("e", {"loop": 900, "setup": 200}),
        with_symbols("e", {"loop": 400, "setup": 200}),
    )
    assert rows == [("loop", 400, 900, 500)]


def test_symbols_added_and_removed_are_both_reported():
    from scripts.size_diff import symbol_deltas

    rows = dict((r[0], r[3]) for r in symbol_deltas(
        with_symbols("e", {"added": 300}), with_symbols("e", {"gone": 128})))
    assert rows == {"added": 300, "gone": -128}


def test_symbol_rows_are_ordered_by_magnitude_then_name():
    from scripts.size_diff import symbol_deltas

    rows = symbol_deltas(
        with_symbols("e", {"a": 100, "b": 500, "c": 100}),
        with_symbols("e", {"a": 0, "b": 0, "c": 0}),
    )
    assert [r[0] for r in rows] == ["b", "a", "c"]


def test_a_manifest_without_symbols_yields_no_symbol_rows():
    from scripts.size_diff import symbol_deltas

    assert symbol_deltas(manifest("e", 100), manifest("e", 100)) == []
    assert symbol_deltas(with_symbols("e", {"x": 1}), None) == []


def test_truncation_is_disclosed_when_either_side_was_truncated():
    d = diff_env("e", with_symbols("e", {"x": 2}, truncated=True),
                 with_symbols("e", {"x": 1}))
    body = render([d], "abc", 2048, [])
    assert "Only the largest symbols are recorded" in body


def test_no_truncation_note_when_both_lists_are_complete():
    d = diff_env("e", with_symbols("e", {"x": 2}), with_symbols("e", {"x": 1}))
    body = render([d], "abc", 2048, [])
    assert "Only the largest symbols are recorded" not in body


def test_a_pipe_in_a_symbol_name_cannot_break_the_table():
    d = diff_env("e", with_symbols("e", {"op|weird": 200}),
                 with_symbols("e", {"op|weird": 100}))
    body = render([d], "abc", 2048, [])
    # Escaped, so the pipe renders as text instead of opening a sixth cell.
    assert r"op\|weird" in body


def test_demangling_falls_back_to_the_raw_name_when_cxxfilt_is_missing(monkeypatch):
    import scripts.size_diff as sd

    def boom(*a, **k):
        raise OSError("no c++filt here")

    monkeypatch.setattr(sd.subprocess, "run", boom)
    assert sd.demangle(["_ZN5Blink6updateEj"]) == {"_ZN5Blink6updateEj": "_ZN5Blink6updateEj"}


def test_demangling_refuses_a_mismatched_response(monkeypatch):
    import scripts.size_diff as sd

    class Result:
        stdout = "only one line\n"

    monkeypatch.setattr(sd.subprocess, "run", lambda *a, **k: Result())
    # Two names in, one line out: pairing them would mislabel a symbol.
    assert sd.demangle(["a", "b"]) == {"a": "a", "b": "b"}


def test_the_ram_caveat_is_stated_in_the_footer():
    body = render([diff_env("e", manifest("e", 100), manifest("e", 100))], "abc", 2048, [])
    assert "heap or stack" in body
