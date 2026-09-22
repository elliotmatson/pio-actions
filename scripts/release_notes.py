#!/usr/bin/env python3
"""Render a repo-owned markdown template into a release description.

GitHub's generated notes are a list of merged pull requests. A firmware release
also wants the same handful of paragraphs every time: which file to flash, what
the -factory image is for, where the sizes landed. Keeping those in a template
file in the repository means they are reviewed like anything else, instead of
being retyped into the release form.

The rendered template is prepended to the generated notes by the release step.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

try:  # importable as a package (tests) or run as a script (the workflow)
    from .size_diff import load_manifests
    from .size_report import human
except ImportError:  # pragma: no cover - exercised by the CLI path
    from size_diff import load_manifests
    from size_report import human


def size_table(manifests: dict[str, dict]) -> str:
    """The per-environment size table, or "" when nothing was measured."""
    if not manifests:
        return ""
    rows = [
        "| Environment | Image | App partition | Flash | RAM |",
        "| --- | --- | --- | --- | --- |",
    ]
    for env in sorted(manifests):
        m = manifests[env]
        pct = m.get("partition_pct")
        ceiling = m.get("app_partition_bytes")
        fit = "n/a" if pct is None else f"{pct}% of {human(ceiling)}"
        rows.append(
            f"| `{env}` | {human(m.get('bin_bytes'))} | {fit} | "
            f"{human(m.get('flash_bytes'))} | {human(m.get('ram_bytes'))} |"
        )
    return "\n".join(rows)


def env_list(envs: str, manifests: dict[str, dict]) -> str:
    """`a`, `b` from the discover output, falling back to what was measured."""
    names: list[str] = []
    if envs:
        try:
            parsed = json.loads(envs)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            names = [str(n) for n in parsed]
    if not names:
        names = sorted(manifests)
    return ", ".join(f"`{n}`" for n in names)


def render(template: str, tokens: dict[str, str]) -> str:
    """Substitute {token}s. Anything unrecognised is left alone -- a template
    may legitimately contain braces, in a code fence or a JSON example."""
    for name, value in tokens.items():
        template = template.replace("{%s}" % name, value)
    return template


def build_tokens(version: str, release_type: str, repo: str, sha: str,
                 envs: str, manifests: dict[str, dict],
                 today: str = "") -> dict[str, str]:
    return {
        "version": version,
        "type": release_type,
        "repo": repo,
        "sha": sha,
        "short_sha": sha[:7],
        "date": today or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "envs": env_list(envs, manifests),
        "sizes": size_table(manifests),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", required=True)
    ap.add_argument("--manifests", default="", help="directory of size manifests")
    ap.add_argument("--envs", default="", help="JSON array of environments built")
    ap.add_argument("--version", default="")
    ap.add_argument("--type", dest="release_type", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--sha", default="")
    ap.add_argument("--output", default="release-notes.md")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.template):
        print(f"::error::release notes template not found: {args.template}")
        return 1

    with open(args.template, encoding="utf-8") as fh:
        template = fh.read()

    manifests = load_manifests(args.manifests) if args.manifests else {}
    body = render(template, build_tokens(
        args.version, args.release_type, args.repo, args.sha, args.envs, manifests,
    ))

    if not body.strip():
        # Not fatal: the release still publishes with the generated notes. But
        # a template that renders to nothing is a mistake, not a preference.
        print(f"::warning::{args.template} rendered empty; the release keeps "
              "GitHub's generated notes only")
        return 0

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(body.rstrip() + "\n")
    print(f"wrote {args.output} ({len(body)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
