#!/usr/bin/env python3
"""Turn `pio test --junit-output-path` into pull-request feedback.

Unity prints a pass/fail line per assertion and PlatformIO summarises to the
console, but neither lands where a reviewer looks. The JUnit document carries
the file and line of each case, which is enough to annotate the failing
assertion on the diff.

A run that executed no tests is treated as a failure. A test step that passes
because it never ran is the same defect as a lint step reporting a clean file
it never opened.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET

MARKER = "<!-- pio-actions:tests -->"


def parse(path: str) -> dict:
    """Read the JUnit document into suites and cases."""
    root = ET.parse(path).getroot()
    suites = []
    for suite in root.iter("testsuite"):
        cases = []
        for case in suite.iter("testcase"):
            failure = case.find("failure")
            error = case.find("error")
            bad = failure if failure is not None else error
            cases.append({
                "name": case.get("name", "?"),
                "status": (case.get("status") or "").upper(),
                "time": float(case.get("time") or 0.0),
                "file": case.get("file"),
                "line": int(case.get("line") or 0),
                "kind": "error" if error is not None else ("failure" if failure is not None else None),
                "message": (bad.get("message") if bad is not None else None)
                or (bad.text.strip() if bad is not None and bad.text else None),
            })
        suites.append({
            "name": suite.get("name", "?"),
            "time": float(suite.get("time") or 0.0),
            "cases": cases,
        })
    return {"suites": suites}


def tally(report: dict) -> dict[str, int]:
    counts = {"tests": 0, "passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    for suite in report["suites"]:
        for case in suite["cases"]:
            counts["tests"] += 1
            if case["kind"] == "error":
                counts["errored"] += 1
            elif case["kind"] == "failure":
                counts["failed"] += 1
            elif case["status"] == "SKIPPED":
                counts["skipped"] += 1
            else:
                counts["passed"] += 1
    return counts


def annotations(report: dict, root: str = ".") -> list[str]:
    """One annotation per failing case, on the assertion that failed."""
    out = []
    for suite in report["suites"]:
        for case in suite["cases"]:
            if not case["kind"]:
                continue
            message = (case["message"] or "test failed").replace("\n", " ").replace("%", "%25")
            title = f"{suite['name']} :: {case['name']}"
            parts = []
            if case["file"]:
                path = case["file"]
                if os.path.isabs(path):
                    try:
                        rel = os.path.relpath(path, os.path.abspath(root))
                        path = path if rel.startswith("..") else rel
                    except ValueError:
                        pass
                parts.append(f"file={path}")
                if case["line"]:
                    parts.append(f"line={case['line']}")
            parts.append(f"title={title}")
            out.append(f"::error {','.join(parts)}::{message}")
    return out


def render_markdown(report: dict, counts: dict[str, int]) -> str:
    lines = [MARKER, "### Tests", ""]

    if not counts["tests"]:
        lines.append("**No tests ran.** The run produced no results, which means "
                     "the suite did not execute rather than that it passed.")
        return "\n".join(lines)

    bad = counts["failed"] + counts["errored"]
    headline = (f"**{counts['passed']}/{counts['tests']} passed**"
                if not bad else
                f"**{bad} of {counts['tests']} failed**")
    if counts["skipped"]:
        headline += f", {counts['skipped']} skipped"
    lines.append(headline + ".")
    lines.append("")

    lines.append("| Suite | Tests | Passed | Failed | Time |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for suite in report["suites"]:
        n = len(suite["cases"])
        # PlatformIO emits a suite for every environment/test pair in the
        # project, including the ones this run did not select. Listing them as
        # zero-test rows reads as "ran and found nothing", which is the very
        # ambiguity the no-tests-ran guard exists to remove.
        if not n:
            continue
        failed = sum(1 for c in suite["cases"] if c["kind"])
        lines.append(f"| `{suite['name']}` | {n} | {n - failed} | {failed} | {suite['time']:.2f}s |")
    lines.append("")

    failures = [(s, c) for s in report["suites"] for c in s["cases"] if c["kind"]]
    if failures:
        lines.append("<details open><summary>Failures</summary>")
        lines.append("")
        for suite, case in failures[:20]:
            where = f"`{case['file']}:{case['line']}`" if case["file"] else ""
            message = (case["message"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"- **{suite['name']} :: {case['name']}** {where}  \n  {message}")
        if len(failures) > 20:
            lines.append(f"- _+{len(failures) - 20} more_")
        lines.append("")
        lines.append("</details>")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="JUnit XML from `pio test`")
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--comment", default="")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        raise SystemExit(f"no test report at {args.input}; `pio test` did not run")

    report = parse(args.input)
    counts = tally(report)

    for line in annotations(report, args.project_dir):
        print(line)

    body = render_markdown(report, counts)
    if args.comment:
        with open(args.comment, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            for key, value in counts.items():
                fh.write(f"{key}={value}\n")

    if not counts["tests"]:
        print("::error::no tests ran; the suite did not execute")
        return 1
    if counts["failed"] or counts["errored"]:
        print(f"::error::{counts['failed'] + counts['errored']} test(s) failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
