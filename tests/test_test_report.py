import pytest

from scripts.test_report import MARKER, annotations, parse, render_markdown, tally

ROOT = "/home/runner/work/proj/proj"

PASSING = """<?xml version="1.0"?>
<testsuites name="proj" tests="3" errors="0" failures="0" time="0.9">
  <testsuite name="native:test_blink" tests="3" errors="0" failures="0" skipped="0" time="0.9">
    <testcase name="starts_off" time="0.1" status="PASSED" file="test/test_blink/test_blink.cpp" line="20"/>
    <testcase name="turns_on" time="0.3" status="PASSED" file="test/test_blink/test_blink.cpp" line="27"/>
    <testcase name="survives_rollover" time="0.5" status="PASSED" file="test/test_blink/test_blink.cpp" line="41"/>
  </testsuite>
</testsuites>
"""

FAILING = """<?xml version="1.0"?>
<testsuites name="proj" tests="2" errors="1" failures="1" time="0.4">
  <testsuite name="native:test_blink" tests="2" errors="1" failures="1" skipped="0" time="0.4">
    <testcase name="survives_rollover" time="0.2" status="FAILED" file="test/test_blink/test_blink.cpp" line="41">
      <failure message="Expected FALSE Was TRUE"/>
    </testcase>
    <testcase name="crashes" time="0.2" status="ERRORED">
      <error type="TestRunnerError" message="program exited with code 139"/>
    </testcase>
  </testsuite>
</testsuites>
"""

EMPTY = """<?xml version="1.0"?>
<testsuites name="proj" tests="0" errors="0" failures="0" time="0.0"/>
"""


def write(tmp_path, body):
    p = tmp_path / "results.xml"
    p.write_text(body)
    return str(p)


# --- parsing -----------------------------------------------------------------

def test_a_passing_run_is_counted(tmp_path):
    counts = tally(parse(write(tmp_path, PASSING)))
    assert counts == {"tests": 3, "passed": 3, "failed": 0, "errored": 0, "skipped": 0}


def test_failures_and_errors_are_distinguished(tmp_path):
    counts = tally(parse(write(tmp_path, FAILING)))
    assert counts["failed"] == 1 and counts["errored"] == 1 and counts["passed"] == 0


def test_a_run_with_no_cases_counts_nothing(tmp_path):
    assert tally(parse(write(tmp_path, EMPTY)))["tests"] == 0


# --- annotations -------------------------------------------------------------

def test_only_failing_cases_are_annotated(tmp_path):
    assert annotations(parse(write(tmp_path, PASSING))) == []
    assert len(annotations(parse(write(tmp_path, FAILING)))) == 2


def test_an_annotation_points_at_the_failing_assertion(tmp_path):
    first = annotations(parse(write(tmp_path, FAILING)))[0]
    assert first.startswith("::error ")
    assert "file=test/test_blink/test_blink.cpp" in first
    assert "line=41" in first
    assert "Expected FALSE Was TRUE" in first


def test_a_case_without_a_source_location_still_annotates(tmp_path):
    # The errored case carries no file; it must not be dropped.
    joined = "\n".join(annotations(parse(write(tmp_path, FAILING))))
    assert "program exited with code 139" in joined


def test_absolute_paths_are_made_relative(tmp_path):
    body = FAILING.replace('file="test/', f'file="{ROOT}/test/')
    first = annotations(parse(write(tmp_path, body)), ROOT)[0]
    assert "file=test/test_blink/test_blink.cpp" in first


def test_a_newline_in_a_message_cannot_break_the_command(tmp_path):
    body = FAILING.replace("Expected FALSE Was TRUE", "line one&#10;line two")
    assert "\n" not in annotations(parse(write(tmp_path, body)))[0]


# --- rendering ---------------------------------------------------------------

def test_the_comment_is_stickily_marked(tmp_path):
    body = render_markdown(parse(write(tmp_path, PASSING)), tally(parse(write(tmp_path, PASSING))))
    assert body.startswith(MARKER)


def test_a_passing_run_reads_as_passing(tmp_path):
    report = parse(write(tmp_path, PASSING))
    assert "**3/3 passed**" in render_markdown(report, tally(report))


def test_a_failing_run_leads_with_the_failure_count(tmp_path):
    report = parse(write(tmp_path, FAILING))
    body = render_markdown(report, tally(report))
    assert "**2 of 2 failed**" in body
    assert "survives_rollover" in body and "test/test_blink/test_blink.cpp:41" in body


def test_an_empty_run_says_it_did_not_execute(tmp_path):
    report = parse(write(tmp_path, EMPTY))
    body = render_markdown(report, tally(report))
    # The lesson from the analysis workflow: no results is not a pass.
    assert "No tests ran" in body


# --- exit status -------------------------------------------------------------

def _run(tmp_path, body=None, path=None):
    import subprocess
    import sys

    target = path if path is not None else write(tmp_path, body)
    return subprocess.run(
        [sys.executable, "scripts/test_report.py", "--input", target],
        capture_output=True, text=True,
    )


def test_a_passing_run_exits_zero(tmp_path):
    assert _run(tmp_path, PASSING).returncode == 0


def test_a_failing_run_exits_non_zero(tmp_path):
    assert _run(tmp_path, FAILING).returncode == 1


def test_a_run_with_no_tests_fails_rather_than_passing(tmp_path):
    done = _run(tmp_path, EMPTY)
    assert done.returncode == 1
    assert "no tests ran" in (done.stdout + done.stderr)


def test_a_missing_report_fails_loudly(tmp_path):
    done = _run(tmp_path, path=str(tmp_path / "nope.xml"))
    assert done.returncode != 0
    assert "did not run" in (done.stdout + done.stderr)
