#!/usr/bin/env python3
"""Compare firmware size manifests and render a pull-request comment.

Bytes on their own do not tell a reviewer anything actionable. What matters is
whether a change moved the image closer to the partition ceiling, and which
sections moved. This renders both, plus a per-section breakdown for the
environments that actually changed.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

try:  # importable as a package (tests) or run as a script (the workflow)
    from .size_report import human
except ImportError:  # pragma: no cover - exercised by the CLI path
    from size_report import human

MARKER = "<!-- pio-actions:size-diff -->"
MAX_SECTION_ROWS = 12
MAX_SYMBOL_ROWS = 15


def load_manifests(directory: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(directory, "*-manifest.json"))):
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        out[manifest["env"]] = manifest
    return out


def signed(n: int | None) -> str:
    if n is None:
        return "n/a"
    if n == 0:
        return "0"
    return f"{'+' if n > 0 else '-'}{human(abs(n))}"


def _delta(head: dict, base: dict | None, key: str) -> int | None:
    if base is None or head.get(key) is None or base.get(key) is None:
        return None
    return head[key] - base[key]


def section_deltas(head: dict, base: dict | None) -> list[tuple[str, int, int, int]]:
    """Per-section (name, base, head, delta), biggest movers first."""
    if base is None:
        return []
    head_s = head.get("sections") or {}
    base_s = base.get("sections") or {}
    rows = []
    for name in sorted(set(head_s) | set(base_s)):
        h = head_s.get(name, {}).get("size", 0)
        b = base_s.get(name, {}).get("size", 0)
        if h != b:
            rows.append((name, b, h, h - b))
    rows.sort(key=lambda r: abs(r[3]), reverse=True)
    return rows


def demangle(names: list[str]) -> dict[str, str]:
    """Map mangled C++ names to readable ones, in one c++filt pass.

    Best effort: binutils is present on the runners, but a host without it
    should get mangled names rather than no report.
    """
    if not names:
        return {}
    try:
        done = subprocess.run(
            ["c++filt"], input="\n".join(names),
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {n: n for n in names}

    out = done.stdout.splitlines()
    if len(out) != len(names):  # refuse to mispair names with their demangling
        return {n: n for n in names}
    return dict(zip(names, out))


def symbol_deltas(head: dict, base: dict | None) -> list[tuple[str, int, int, int]]:
    """Per-symbol (name, base, head, delta), biggest movers first."""
    if base is None:
        return []
    head_s = head.get("symbols") or {}
    base_s = base.get("symbols") or {}
    rows = []
    for name in set(head_s) | set(base_s):
        h = head_s.get(name, 0)
        b = base_s.get(name, 0)
        if h != b:
            rows.append((name, b, h, h - b))
    rows.sort(key=lambda r: (-abs(r[3]), r[0]))
    return rows


def diff_env(env: str, head: dict, base: dict | None) -> dict:
    return {
        "env": env,
        "head": head,
        "base": base,
        "bin_delta": _delta(head, base, "bin_bytes"),
        "flash_delta": _delta(head, base, "flash_bytes"),
        "ram_delta": _delta(head, base, "ram_bytes"),
        "sections": section_deltas(head, base),
        "symbols": symbol_deltas(head, base),
        "symbols_truncated": bool(head.get("symbols_truncated")
                                  or (base or {}).get("symbols_truncated")),
    }


def verdicts(diffs: list[dict], warn_bytes: int, fail_pct: float) -> list[str]:
    """Conditions that should fail the job, as human-readable strings."""
    problems = []
    for d in diffs:
        pct = d["head"].get("partition_pct")
        if pct is None:
            continue
        if pct >= 100:
            problems.append(
                f"{d['env']}: image is {pct}% of the app partition and will not flash")
        elif pct >= fail_pct:
            problems.append(
                f"{d['env']}: image is {pct}% of the app partition (limit {fail_pct}%)")
    return problems


def _pct_cell(d: dict) -> str:
    head_pct = d["head"].get("partition_pct")
    if head_pct is None:
        return "n/a"
    base_pct = (d["base"] or {}).get("partition_pct")
    if base_pct is None or base_pct == head_pct:
        return f"{head_pct}%"
    return f"{base_pct}% → {head_pct}%"


def render(diffs: list[dict], base_label: str, warn_bytes: int,
           problems: list[str]) -> str:
    lines = [MARKER, "### Firmware size", ""]

    known = [d for d in diffs if d["bin_delta"] is not None]
    if not known:
        lines.append("No baseline to compare against; reporting absolute sizes.")
        lines.append("")
    else:
        total = sum(d["bin_delta"] for d in known)
        biggest = max(abs(d["bin_delta"]) for d in known)
        if biggest == 0:
            lines.append("**No change** to any image.")
        else:
            lines.append(f"**{signed(total)}** across {len(known)} "
                         f"environment{'s' if len(known) != 1 else ''}.")
        lines.append("")

    lines.append("| Env | Image | Δ | App partition | Flash Δ | RAM Δ |")
    lines.append("| --- | ---: | ---: | --- | ---: | ---: |")
    for d in diffs:
        flag = ""
        if d["bin_delta"] is not None and abs(d["bin_delta"]) >= warn_bytes:
            flag = " ⚠️"
        elif d["base"] is None:
            flag = " *(new)*"
        lines.append(
            f"| `{d['env']}`{flag} | {human(d['head'].get('bin_bytes'))} | "
            f"{signed(d['bin_delta'])} | {_pct_cell(d)} | "
            f"{signed(d['flash_delta'])} | {signed(d['ram_delta'])} |"
        )
    lines.append("")

    for d in diffs:
        if not d["sections"]:
            continue
        rows = d["sections"][:MAX_SECTION_ROWS]
        hidden = len(d["sections"]) - len(rows)
        lines.append(f"<details><summary><code>{d['env']}</code> — "
                     f"{len(d['sections'])} section(s) changed</summary>")
        lines.append("")
        lines.append("| Section | Base | Head | Δ |")
        lines.append("| --- | ---: | ---: | ---: |")
        for name, b, h, delta in rows:
            lines.append(f"| `{name}` | {human(b)} | {human(h)} | {signed(delta)} |")
        if hidden:
            lines.append(f"| _+{hidden} more_ | | | |")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    for d in diffs:
        if not d["symbols"]:
            continue
        rows = d["symbols"][:MAX_SYMBOL_ROWS]
        pretty = demangle([r[0] for r in rows])
        hidden = len(d["symbols"]) - len(rows)
        lines.append(f"<details><summary><code>{d['env']}</code> — "
                     f"{len(d['symbols'])} symbol(s) changed</summary>")
        lines.append("")
        lines.append("| Symbol | Base | Head | Δ |")
        lines.append("| --- | ---: | ---: | ---: |")
        for name, b, h, delta in rows:
            label = pretty.get(name, name).replace("|", "\\|")
            lines.append(f"| `{label}` | {human(b)} | {human(h)} | {signed(delta)} |")
        if hidden:
            lines.append(f"| _+{hidden} more_ | | | |")
        lines.append("")
        if d["symbols_truncated"]:
            lines.append("Only the largest symbols are recorded, so one near that "
                         "cutoff can appear here without having changed.")
            lines.append("")
        lines.append("</details>")
        lines.append("")

    if problems:
        lines.append("> [!CAUTION]")
        for p in problems:
            lines.append(f"> {p}")
        lines.append("")

    lines.append(
        f"<sub>Baseline: {base_label}. Image size is `firmware.bin` against the "
        f"smaller app partition slot. RAM is static allocation only — the "
        f"linker cannot see heap or stack.</sub>")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--head", required=True, help="directory of head manifests")
    ap.add_argument("--base", default="", help="directory of base manifests")
    ap.add_argument("--base-label", default="the base branch")
    ap.add_argument("--warn-bytes", type=int, default=2048)
    ap.add_argument("--fail-pct", type=float, default=95.0)
    ap.add_argument("--output", default="size-comment.md")
    args = ap.parse_args()

    head = load_manifests(args.head)
    if not head:
        raise SystemExit(f"no manifests found in {args.head}")
    base = load_manifests(args.base) if args.base else {}

    diffs = [diff_env(env, head[env], base.get(env)) for env in sorted(head)]
    problems = verdicts(diffs, args.warn_bytes, args.fail_pct)
    body = render(diffs, args.base_label, args.warn_bytes, problems)

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")

    print(body)
    if problems:
        for p in problems:
            print(f"::error::{p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
