#!/usr/bin/env python3
"""List the PlatformIO environments a build should cover.

Reads platformio.ini directly instead of shelling out to `pio project config`,
so discovery costs no toolchain install and can run before setup-pio. Section
names are literal, so a raw parser reads them without having to expand
${env.*} interpolation or `extends`.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys


def parse_ini(text: str) -> tuple[list[str], list[str]]:
    """Return (all env names, default_envs) from a platformio.ini body."""
    parser = configparser.RawConfigParser(strict=False)
    # PlatformIO allows bare keys and ${...} values; neither is our business here.
    parser.read_string(text)

    envs = [s[len("env:"):] for s in parser.sections() if s.startswith("env:")]

    defaults: list[str] = []
    if parser.has_option("platformio", "default_envs"):
        raw = parser.get("platformio", "default_envs")
        defaults = [e.strip() for e in raw.replace(",", "\n").split("\n") if e.strip()]

    return envs, defaults


def select(envs: list[str], defaults: list[str], selection: str) -> list[str]:
    """Resolve a selection string against the envs a project declares.

    "default" mirrors what a bare `pio run` builds: default_envs when the
    project sets it, every env otherwise. Anything else is an explicit list,
    which is validated so a typo fails the job instead of silently building
    nothing.
    """
    selection = (selection or "default").strip()

    if selection == "all":
        return envs
    if selection == "default":
        return defaults or envs

    wanted = [e.strip() for e in selection.replace(",", " ").split() if e.strip()]
    unknown = [e for e in wanted if e not in envs]
    if unknown:
        raise SystemExit(
            f"unknown environment(s): {', '.join(unknown)}. "
            f"platformio.ini declares: {', '.join(envs) or '(none)'}"
        )
    return wanted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--select", default="default",
                    help="'default', 'all', or an explicit list of env names")
    args = ap.parse_args()

    ini_path = os.path.join(args.project_dir, "platformio.ini")
    if not os.path.isfile(ini_path):
        raise SystemExit(f"no platformio.ini at {ini_path}")

    with open(ini_path, encoding="utf-8") as fh:
        envs, defaults = parse_ini(fh.read())

    if not envs:
        raise SystemExit(f"{ini_path} declares no [env:*] sections")

    chosen = select(envs, defaults, args.select)
    payload = json.dumps(chosen)

    print(payload)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"envs={payload}\n")
            fh.write(f"count={len(chosen)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
