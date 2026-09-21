#!/usr/bin/env python3
"""Measure what a PlatformIO build costs in flash and RAM.

Two numbers matter and they are not the same one:

  * firmware.bin on disk is what has to fit in the app partition. It carries
    the image header, per-segment headers, padding and checksum that the ELF
    does not, so it is the only honest input to a "percent of partition" figure.
  * The ELF section table is what explains a change. It attributes growth to
    .text / .rodata / .bss rather than to one opaque total.

This reports both, plus the partition ceiling parsed out of the project's
partition table, and emits a manifest the release and memory-diff workflows
both consume.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import io
import json
import os
import sys

# ELF section header flags (ELF spec, Figure 1-13).
SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4


def read_elf_sections(path: str) -> list[dict]:
    """Normalize an ELF's section headers into plain dicts."""
    from elftools.elf.elffile import ELFFile  # imported late: only builds need it

    out = []
    with open(path, "rb") as fh:
        for section in ELFFile(fh).iter_sections():
            flags = section["sh_flags"]
            out.append({
                "name": section.name,
                "size": section["sh_size"],
                "alloc": bool(flags & SHF_ALLOC),
                "write": bool(flags & SHF_WRITE),
                "exec": bool(flags & SHF_EXECINSTR),
                "progbits": section["sh_type"] == "SHT_PROGBITS",
            })
    return out


def classify_sections(sections: list[dict]) -> dict:
    """Split allocated sections into flash cost and RAM cost.

    RAM is everything writable that the loader allocates -- .data, .bss and the
    ESP32's .dram0.* siblings. Flash is everything allocated that carries
    initialized content. .data satisfies both, and is deliberately counted
    twice: it ships in the image *and* occupies RAM at runtime. PlatformIO's own
    size report draws the line the same way, so the totals here line up with
    what `pio run -t size` prints.
    """
    flash = ram = 0
    detail: dict[str, dict[str, int]] = {}

    for s in sections:
        if not s["alloc"] or not s["size"]:
            continue
        in_ram = s["write"]
        in_flash = s["progbits"]
        if not (in_ram or in_flash):
            continue
        flash += s["size"] if in_flash else 0
        ram += s["size"] if in_ram else 0
        detail[s["name"]] = {
            "size": s["size"],
            "flash": s["size"] if in_flash else 0,
            "ram": s["size"] if in_ram else 0,
        }

    return {"flash_bytes": flash, "ram_bytes": ram, "sections": detail}


def parse_size(value: str) -> int:
    """Parse a partition-table size: 0x1f0000, 1M, 1500K or a bare decimal."""
    v = (value or "").strip()
    if not v:
        raise ValueError("empty size")
    mult = 1
    if v[-1] in "kKmM":
        mult = 1024 if v[-1] in "kK" else 1024 * 1024
        v = v[:-1]
    return int(v, 0) * mult


def parse_partition_csv(text: str) -> list[dict]:
    """Parse an ESP-IDF partition table into rows, skipping comments."""
    rows = []
    stripped = "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    for raw in csv.reader(io.StringIO(stripped)):
        fields = [f.strip() for f in raw]
        if len(fields) < 5:
            continue
        try:
            size = parse_size(fields[4])
        except ValueError:
            continue
        rows.append({
            "name": fields[0],
            "type": fields[1].lower(),
            "subtype": fields[2].lower(),
            "size": size,
        })
    return rows


def app_partition_bytes(rows: list[dict]) -> int | None:
    """The app slot the firmware actually has to fit in.

    A dual-OTA table has app0 and app1 and an update must fit whichever is
    smaller, so the minimum is the real ceiling, not the sum and not the first.
    """
    app = [r["size"] for r in rows if r["type"] == "app"]
    return min(app) if app else None


def resolve_partition_table(spec: str, project_dir: str) -> str | None:
    """Find the partition CSV, whether it is in the repo or shipped by the platform.

    board_build.partitions is often a bare name like huge_app.csv that lives
    inside the installed framework, not the project, so fall back to searching
    the PlatformIO package tree before giving up.
    """
    if not spec:
        return None

    local = os.path.join(project_dir, spec)
    if os.path.isfile(local):
        return local

    home = os.environ.get("PLATFORMIO_CORE_DIR") or os.path.expanduser("~/.platformio")
    patterns = [
        os.path.join(home, "packages", "framework-arduinoespressif32*", "tools", "partitions", spec),
        os.path.join(home, "packages", "framework-espidf*", "components", "partition_table", spec),
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def partitions_spec_for_env(project_dir: str, env: str) -> str:
    """Read board_build.partitions for an env, falling back to the [env] base."""
    import configparser

    parser = configparser.RawConfigParser(strict=False)
    ini = os.path.join(project_dir, "platformio.ini")
    if not os.path.isfile(ini):
        return ""
    with open(ini, encoding="utf-8") as fh:
        parser.read_string(fh.read())

    for section in (f"env:{env}", "env"):
        if parser.has_option(section, "board_build.partitions"):
            return parser.get(section, "board_build.partitions").strip()
    return ""


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(build_dir: str, env: str, project_dir: str = ".",
                   partitions: str = "") -> dict:
    elf = os.path.join(build_dir, "firmware.elf")
    binary = os.path.join(build_dir, "firmware.bin")

    manifest: dict = {"env": env, "build_dir": build_dir}

    if os.path.isfile(elf):
        manifest.update(classify_sections(read_elf_sections(elf)))
    else:
        manifest.update({"flash_bytes": None, "ram_bytes": None, "sections": {}})

    manifest["bin_bytes"] = os.path.getsize(binary) if os.path.isfile(binary) else None

    spec = partitions or partitions_spec_for_env(project_dir, env)
    table = resolve_partition_table(spec, project_dir)
    app_bytes = None
    if table:
        with open(table, encoding="utf-8") as fh:
            app_bytes = app_partition_bytes(parse_partition_csv(fh.read()))
    manifest["partition_table"] = table
    manifest["app_partition_bytes"] = app_bytes

    if app_bytes and manifest["bin_bytes"]:
        manifest["partition_pct"] = round(100.0 * manifest["bin_bytes"] / app_bytes, 2)
    else:
        manifest["partition_pct"] = None

    manifest["artifacts"] = {
        os.path.basename(p): {"bytes": os.path.getsize(p), "sha256": sha256(p)}
        for p in sorted(glob.glob(os.path.join(build_dir, "*.bin")))
    }
    return manifest


def human(n: int | None) -> str:
    if n is None:
        return "n/a"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def render_markdown(manifest: dict) -> str:
    pct = manifest.get("partition_pct")
    ceiling = manifest.get("app_partition_bytes")
    if pct is None:
        fit = "no partition table found"
    else:
        fit = f"{pct}% of {human(ceiling)} app partition"
    return (
        f"**{manifest['env']}** — image {human(manifest.get('bin_bytes'))} "
        f"({fit}), flash {human(manifest.get('flash_bytes'))}, "
        f"RAM {human(manifest.get('ram_bytes'))}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-dir", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--partitions", default="",
                    help="override board_build.partitions")
    ap.add_argument("--output", default="", help="write the manifest JSON here")
    ap.add_argument("--summary", action="store_true",
                    help="append a one-line summary to $GITHUB_STEP_SUMMARY")
    args = ap.parse_args()

    manifest = build_manifest(args.build_dir, args.env, args.project_dir, args.partitions)
    text = json.dumps(manifest, indent=2, sort_keys=True)

    if args.output:
        parent = os.path.dirname(args.output)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)

    line = render_markdown(manifest)
    print(line, file=sys.stderr)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if args.summary and summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(line + "\n\n")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            for key in ("bin_bytes", "flash_bytes", "ram_bytes", "partition_pct"):
                fh.write(f"{key}={manifest.get(key)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
