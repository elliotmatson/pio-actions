#!/usr/bin/env python3
"""Turn `pio check --json-output` into pull-request feedback.

Code scanning would be the natural home for this, but it is a paid feature on
private repositories -- which is what firmware repositories usually are. So the
default output is what costs nothing and lands where a reviewer is already
looking: inline annotations on the diff, a summary on the run, and a comment on
the pull request. SARIF is still emitted on request for repositories that do
have code scanning.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

MARKER = "<!-- pio-actions:static-analysis -->"

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}
ANNOTATION_LEVEL = {"high": "error", "medium": "warning", "low": "notice"}
SARIF_LEVEL = {"high": "error", "medium": "warning", "low": "note"}


def load_results(text: str) -> list[dict]:
    """Parse the JSON document out of `pio check --json-output` output.

    The tool manager writes installation progress to the same stream, so the
    JSON is not reliably the whole of stdout. It is emitted as a single line,
    so fall back to scanning backwards for the last line that parses.
    """
    text = text.strip()
    if not text:
        # Not a clean bill of health. `pio check --json-output` always emits a
        # document when it runs, so nothing at all means it never got that far
        # -- an unresolvable platform, a failed install, a crash. Reporting
        # "no defects found" there is the worst thing a lint step can do.
        raise SystemExit("`pio check` produced no output, so it did not run")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise SystemExit("no JSON document found in the `pio check` output")


def relativize(path: str, root: str) -> str:
    """Make a defect path repo-relative so annotations land on the right line."""
    if not path or path == "unknown":
        return "unknown"
    try:
        rel = os.path.relpath(path, root)
    except ValueError:  # different drive on Windows
        return path
    return path if rel.startswith("..") else rel


def collect_defects(results: list[dict], root: str = ".") -> list[dict]:
    """Flatten per-environment results into one deduplicated list.

    A header included by three environments yields the same defect three times.
    Reporting it once, tagged with the environments it came from, keeps the
    annotation count honest.
    """
    merged: dict[tuple, dict] = {}
    for result in results:
        env = result.get("env", "?")
        for defect in result.get("defects", []):
            severity = (defect.get("severity") or "low").lower()
            key = (
                relativize(defect.get("file", ""), root),
                defect.get("line", 0),
                defect.get("column", 0),
                defect.get("id"),
                defect.get("message"),
                severity,
            )
            if key in merged:
                merged[key]["envs"].append(env)
                continue
            merged[key] = {
                "file": key[0],
                "line": int(defect.get("line") or 0),
                "column": int(defect.get("column") or 0),
                "id": defect.get("id") or "",
                "message": defect.get("message") or "",
                "severity": severity,
                "category": defect.get("category") or "",
                "cwe": defect.get("cwe"),
                "envs": [env],
            }

    out = list(merged.values())
    out.sort(key=lambda d: (-SEVERITY_ORDER.get(d["severity"], 0), d["file"], d["line"]))
    return out


def failed_tools(results: list[dict]) -> list[str]:
    """Tools that did not run. A crashed analyser is not a clean report."""
    out = []
    for r in results:
        if r.get("succeeded") is not False:
            continue
        detail = r.get("message") or r.get("error") or ""
        label = f"{r.get('tool', '?')} on {r.get('env', '?')}"
        out.append(f"{label}: {detail}" if detail else label)
    return out


def counts(defects: list[dict]) -> dict[str, int]:
    tally = collections.Counter(d["severity"] for d in defects)
    return {level: tally.get(level, 0) for level in ("high", "medium", "low")}


def annotations(defects: list[dict], limit: int = 50) -> list[str]:
    """Workflow commands GitHub renders inline on the diff."""
    lines = []
    for d in defects[:limit]:
        level = ANNOTATION_LEVEL.get(d["severity"], "notice")
        title = f"{d['id'] or d['category'] or 'defect'} ({d['severity']})"
        # Commas and newlines would terminate the command's parameter list.
        message = d["message"].replace("\n", " ").replace("%", "%25")
        parts = [f"file={d['file']}"]
        if d["line"]:
            parts.append(f"line={d['line']}")
        if d["column"]:
            parts.append(f"col={d['column']}")
        parts.append(f"title={title}")
        lines.append(f"::{level} {','.join(parts)}::{message}")
    return lines


def over_threshold(defects: list[dict], fail_on: str) -> list[dict]:
    """Defects at or above the configured severity."""
    if fail_on == "none":
        return []
    floor = SEVERITY_ORDER[fail_on]
    return [d for d in defects if SEVERITY_ORDER.get(d["severity"], 0) >= floor]


def render_markdown(defects: list[dict], tally: dict[str, int], fail_on: str,
                    broken: list[str], rows: int = 30) -> str:
    lines = [MARKER, "### Static analysis", ""]

    if broken:
        lines.append("> [!CAUTION]")
        lines.append("> These tools did not complete, so the report is incomplete:")
        for tool in broken:
            lines.append(f"> - `{tool}`")
        lines.append("")

    total = sum(tally.values())
    if not total:
        lines.append("No defects found.")
        lines.append("")
        lines.append(f"<sub>`pio check`, failing on `{fail_on}` and above.</sub>")
        return "\n".join(lines)

    lines.append(f"**{total} defect{'s' if total != 1 else ''}** — "
                 f"{tally['high']} high, {tally['medium']} medium, {tally['low']} low.")
    lines.append("")
    lines.append("| Severity | File | Defect |")
    lines.append("| --- | --- | --- |")
    for d in defects[:rows]:
        where = f"`{d['file']}:{d['line']}`" if d["line"] else f"`{d['file']}`"
        rule = f" `{d['id']}`" if d["id"] else ""
        message = d["message"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {d['severity']} | {where} | {message}{rule} |")
    if len(defects) > rows:
        lines.append(f"| | | _+{len(defects) - rows} more_ |")
    lines.append("")
    lines.append(f"<sub>`pio check`, failing on `{fail_on}` and above. "
                 f"Defects seen in several environments are listed once.</sub>")
    return "\n".join(lines)


def to_sarif(defects: list[dict]) -> dict:
    rules: dict[str, dict] = {}
    results = []
    for d in defects:
        rule_id = d["id"] or d["category"] or "defect"
        rules.setdefault(rule_id, {
            "id": rule_id,
            "shortDescription": {"text": rule_id},
            "properties": {"tags": [d["category"]] if d["category"] else []},
        })
        results.append({
            "ruleId": rule_id,
            "level": SARIF_LEVEL.get(d["severity"], "note"),
            "message": {"text": d["message"]},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": d["file"]},
                    "region": {"startLine": max(d["line"], 1),
                               "startColumn": max(d["column"], 1)},
                }
            }],
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "PlatformIO Check",
                "informationUri": "https://docs.platformio.org/en/latest/core/userguide/cmd_check.html",
                "rules": list(rules.values()),
            }},
            "results": results,
        }],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="raw `pio check --json-output` output")
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--fail-on", default="high", choices=["high", "medium", "low", "none"])
    ap.add_argument("--max-annotations", type=int, default=50)
    ap.add_argument("--comment", default="", help="write the comment body here")
    ap.add_argument("--sarif", default="", help="write SARIF here")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as fh:
        results = load_results(fh.read())

    if not results:
        # A document, but describing nothing. Every environment was filtered
        # out or none could be processed; either way nothing was analysed.
        raise SystemExit("`pio check` reported no environments; nothing was analysed")

    root = os.path.abspath(args.project_dir)
    defects = collect_defects(results, root)
    broken = failed_tools(results)
    tally = counts(defects)

    for line in annotations(defects, args.max_annotations):
        print(line)
    if len(defects) > args.max_annotations:
        print(f"::notice::{len(defects) - args.max_annotations} further defects are "
              f"listed in the summary but not annotated")

    body = render_markdown(defects, tally, args.fail_on, broken)
    if args.comment:
        with open(args.comment, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")

    if args.sarif:
        with open(args.sarif, "w", encoding="utf-8") as fh:
            json.dump(to_sarif(defects), fh, indent=2)

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            for level, n in tally.items():
                fh.write(f"{level}={n}\n")
            fh.write(f"total={sum(tally.values())}\n")
            fh.write(f"tool_failures={len(broken)}\n")

    blocking = over_threshold(defects, args.fail_on)
    if broken:
        print(f"::error::{len(broken)} analysis tool(s) failed to run")
        return 1
    if blocking:
        print(f"::error::{len(blocking)} defect(s) at or above '{args.fail_on}' severity")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
