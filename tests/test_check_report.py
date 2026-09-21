import json

import pytest

from scripts.check_report import (
    MARKER,
    annotations,
    collect_defects,
    counts,
    failed_tools,
    load_results,
    over_threshold,
    relativize,
    render_markdown,
    to_sarif,
)

ROOT = "/home/runner/work/proj/proj"


def defect(severity="low", file="src/main.cpp", line=42, column=5,
           id="unusedVariable", message="Unused variable: x", category="style"):
    return {"severity": severity, "category": category, "message": message,
            "file": f"{ROOT}/{file}", "line": line, "column": column,
            "callstack": None, "id": id, "cwe": 563}


def result(env="esp32dev", defects=None, succeeded=True, tool="cppcheck"):
    return {"env": env, "tool": tool, "duration": 1.0,
            "succeeded": succeeded, "defects": defects or []}


# --- parsing the tool output -------------------------------------------------

def test_a_clean_json_document_parses():
    assert load_results(json.dumps([result()])) == [result()]


def test_the_json_is_found_after_tool_manager_chatter():
    # `pio check` shares stdout with the package installer, so the document is
    # not reliably the whole stream.
    noisy = "Tool Manager: Installing cppcheck\nUnpacking 0% 50% 100%\n" + json.dumps([result()])
    assert load_results(noisy)[0]["env"] == "esp32dev"


def test_empty_output_is_an_error_not_a_clean_report():
    # pio check emits a document whenever it runs, so nothing at all means it
    # never ran. Treating that as zero defects reports a passing lint step for
    # a tool that never started -- which is exactly what happened when a
    # platform bump made the pinned PlatformIO incompatible.
    with pytest.raises(SystemExit):
        load_results("   ")


def test_output_with_no_json_fails_loudly():
    with pytest.raises(SystemExit):
        load_results("Error: could not run cppcheck\n")


# --- paths -------------------------------------------------------------------

def test_absolute_paths_become_repo_relative_so_annotations_land():
    assert relativize(f"{ROOT}/src/main.cpp", ROOT) == "src/main.cpp"


def test_a_path_outside_the_project_is_left_alone():
    assert relativize("/usr/include/stdio.h", ROOT) == "/usr/include/stdio.h"


def test_an_unknown_file_stays_unknown():
    assert relativize("unknown", ROOT) == "unknown"


# --- deduplication -----------------------------------------------------------

def test_the_same_defect_in_three_environments_is_reported_once():
    results = [result(env=e, defects=[defect()]) for e in ("a", "b", "c")]
    got = collect_defects(results, ROOT)
    assert len(got) == 1
    assert got[0]["envs"] == ["a", "b", "c"]


def test_defects_differing_only_by_line_are_kept_apart():
    results = [result(defects=[defect(line=10), defect(line=20)])]
    assert len(collect_defects(results, ROOT)) == 2


def test_defects_are_ordered_worst_first():
    results = [result(defects=[
        defect(severity="low", line=1), defect(severity="high", line=2),
        defect(severity="medium", line=3)])]
    assert [d["severity"] for d in collect_defects(results, ROOT)] == ["high", "medium", "low"]


def test_counts_cover_every_level_even_when_absent():
    got = collect_defects([result(defects=[defect(severity="high")])], ROOT)
    assert counts(got) == {"high": 1, "medium": 0, "low": 0}


# --- a crashed analyser is not a clean report --------------------------------

def test_a_tool_that_did_not_run_is_reported():
    assert failed_tools([result(succeeded=False, tool="cppcheck", env="esp32dev")]) \
        == ["cppcheck on esp32dev"]


def test_a_successful_tool_is_not_reported():
    assert failed_tools([result(succeeded=True)]) == []


# --- annotations -------------------------------------------------------------

def test_severity_maps_onto_the_annotation_levels():
    got = collect_defects([result(defects=[
        defect(severity="high", line=1), defect(severity="medium", line=2),
        defect(severity="low", line=3)])], ROOT)
    out = annotations(got)
    assert out[0].startswith("::error ")
    assert out[1].startswith("::warning ")
    assert out[2].startswith("::notice ")


def test_an_annotation_carries_the_relative_path_and_position():
    out = annotations(collect_defects([result(defects=[defect()])], ROOT))
    assert "file=src/main.cpp" in out[0] and "line=42" in out[0] and "col=5" in out[0]


def test_a_newline_in_a_message_cannot_break_the_command():
    got = collect_defects([result(defects=[defect(message="line one\nline two")])], ROOT)
    assert "\n" not in annotations(got)[0]


def test_annotations_are_capped():
    many = [defect(line=n) for n in range(100)]
    assert len(annotations(collect_defects([result(defects=many)], ROOT), limit=10)) == 10


# --- the failure threshold ---------------------------------------------------

@pytest.mark.parametrize("fail_on,expected", [
    ("high", 1),
    ("medium", 2),
    ("low", 3),
    ("none", 0),
])
def test_the_threshold_selects_the_right_defects(fail_on, expected):
    got = collect_defects([result(defects=[
        defect(severity="high", line=1), defect(severity="medium", line=2),
        defect(severity="low", line=3)])], ROOT)
    assert len(over_threshold(got, fail_on)) == expected


# --- rendering ---------------------------------------------------------------

def test_the_comment_is_stickily_marked():
    assert render_markdown([], counts([]), "high", []).startswith(MARKER)


def test_a_clean_run_says_so():
    assert "No defects found." in render_markdown([], counts([]), "high", [])


def test_a_broken_tool_is_called_out_even_with_no_defects():
    body = render_markdown([], counts([]), "high", ["cppcheck on esp32dev"])
    assert "[!CAUTION]" in body and "cppcheck on esp32dev" in body


def test_a_pipe_in_a_message_cannot_break_the_table():
    got = collect_defects([result(defects=[defect(message="a || b")])], ROOT)
    assert r"a \|\| b" in render_markdown(got, counts(got), "high", [])


# --- SARIF -------------------------------------------------------------------

def test_sarif_carries_one_rule_per_defect_id():
    got = collect_defects([result(defects=[
        defect(id="a", line=1), defect(id="a", line=2), defect(id="b", line=3)])], ROOT)
    rules = to_sarif(got)["runs"][0]["tool"]["driver"]["rules"]
    assert sorted(r["id"] for r in rules) == ["a", "b"]


def test_sarif_regions_are_one_based():
    # A zero line would be rejected by the SARIF schema.
    got = collect_defects([result(defects=[defect(line=0, column=0)])], ROOT)
    region = to_sarif(got)["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region["startLine"] == 1 and region["startColumn"] == 1



def test_a_document_describing_no_environments_is_an_error(tmp_path):
    # `[]` parses, but means nothing was analysed.
    import subprocess
    import sys

    raw = tmp_path / "check-raw.txt"
    raw.write_text("[]")
    done = subprocess.run(
        [sys.executable, "scripts/check_report.py", "--input", str(raw)],
        capture_output=True, text=True,
    )
    assert done.returncode != 0
    assert "no environments" in (done.stderr + done.stdout)


def test_a_real_document_still_reports_normally(tmp_path):
    import json
    import subprocess
    import sys

    raw = tmp_path / "check-raw.txt"
    raw.write_text(json.dumps([result(defects=[defect()])]))
    out = tmp_path / "comment.md"
    done = subprocess.run(
        [sys.executable, "scripts/check_report.py", "--input", str(raw),
         "--comment", str(out), "--fail-on", "high"],
        capture_output=True, text=True,
    )
    assert done.returncode == 0
    assert "1 defect" in out.read_text()



# --- defects in downloaded dependencies --------------------------------------

def dep_defect(**kw):
    kw.setdefault("file", "../../.pio/libdeps/hub/ArduinoJson/src/x.hpp")
    return defect(**kw)


def test_a_library_header_is_recognised_as_a_dependency():
    from scripts.check_report import is_dependency

    assert is_dependency(".pio/libdeps/hub/ArduinoJson/src/x.hpp")
    assert is_dependency("examples/blink/.pio/libdeps/e/lib/y.h")


def test_project_sources_are_not_dependencies():
    from scripts.check_report import is_dependency

    assert not is_dependency("src/main.cpp")
    assert not is_dependency("lib/hub/hub.cpp")
    # A project directory that merely starts with the same letters.
    assert not is_dependency(".piohelper/thing.c")


def test_dependency_defects_are_split_out():
    from scripts.check_report import partition_dependencies

    own, deps = partition_dependencies([
        {"file": "src/main.cpp"},
        {"file": ".pio/libdeps/hub/ArduinoJson/src/x.hpp"},
    ])
    assert [d["file"] for d in own] == ["src/main.cpp"]
    assert [d["file"] for d in deps] == [".pio/libdeps/hub/ArduinoJson/src/x.hpp"]


def test_a_high_severity_dependency_defect_does_not_gate_the_merge(tmp_path):
    # The ArduinoJson case: cppcheck cannot parse the header and calls its own
    # parse failure a high-severity defect, in a file the repo does not own.
    import json
    import subprocess
    import sys

    raw = tmp_path / "raw.txt"
    raw.write_text(json.dumps([result(defects=[
        {"severity": "high", "category": "error", "id": "preprocessorErrorDirective",
         "message": "failed to expand 'ARDUINOJSON_BEGIN_PUBLIC_NAMESPACE'",
         "file": f"{ROOT}/.pio/libdeps/hub/ArduinoJson/src/x.hpp",
         "line": 7, "column": 0, "callstack": None, "cwe": None},
    ])]))
    out = tmp_path / "comment.md"
    done = subprocess.run(
        [sys.executable, "scripts/check_report.py", "--input", str(raw),
         "--project-dir", ROOT, "--comment", str(out), "--fail-on", "high"],
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    body = out.read_text()
    assert "No defects found." in body
    # Withheld, not hidden.
    assert "1 defect in downloaded dependencies withheld" in body


def test_include_dependencies_puts_them_back(tmp_path):
    import json
    import subprocess
    import sys

    raw = tmp_path / "raw.txt"
    raw.write_text(json.dumps([result(defects=[
        {"severity": "high", "category": "error", "id": "preprocessorErrorDirective",
         "message": "boom", "file": f"{ROOT}/.pio/libdeps/hub/ArduinoJson/src/x.hpp",
         "line": 7, "column": 0, "callstack": None, "cwe": None},
    ])]))
    done = subprocess.run(
        [sys.executable, "scripts/check_report.py", "--input", str(raw),
         "--project-dir", ROOT, "--fail-on", "high", "--include-dependencies"],
        capture_output=True, text=True,
    )
    assert done.returncode == 1
