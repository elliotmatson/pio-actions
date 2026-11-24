#!/usr/bin/env python3
"""Describe a release's assets for a flashing tool: what each file is and where it goes.

A flasher should not have to parse asset names to learn that one file is a
bootloader and another a partition table, nor know per chip that a bootloader
sits at 0x1000 on an ESP32 and 0x0 on an S3. This writes one release-wide
``manifest.json`` that says so, with each asset's size and sha256.

Everything is read from the staged files themselves, so the script needs no
build tree and can be run against any published release:

  * the chip comes from the ESP image header of the app (or bootloader) image;
  * the app, and any filesystem image, are placed from the partition table
    in ``<env>-<version>-partitions.bin``;
  * where a merged factory image exists, each part's offset is checked against
    where the factory image actually holds it. The factory image is what
    pioarduino flashed together, so on a disagreement it wins and a warning
    says so.

Single file rather than one per environment: the size manifests already
claim ``*-manifest.json`` (size_diff.py and release_notes.py glob for it), and
``<env>-<version>-manifest.json`` would collide with them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys

SCHEMA = 1

ESP_IMAGE_MAGIC = 0xE9
PARTITION_MAGIC = b"\xaa\x50"
PARTITION_MD5_MAGIC = b"\xeb\xeb"
PARTITION_ENTRY = 32
DEFAULT_PARTITION_TABLE_OFFSET = 0x8000
FLASH_SECTOR = 0x1000

# esp_chip_id_t, from the extended image header (bytes 12-13).
CHIP_IDS = {
    0x0000: "esp32",
    0x0002: "esp32s2",
    0x0005: "esp32c3",
    0x0009: "esp32s3",
    0x000C: "esp32c2",
    0x000D: "esp32c6",
    0x0010: "esp32h2",
    0x0012: "esp32p4",
    0x0014: "esp32c61",
    0x0017: "esp32c5",
    0x0019: "esp32h21",
    0x001C: "esp32h4",
}

# Where the ROM loads the second-stage bootloader from (CONFIG_BOOTLOADER_OFFSET_IN_FLASH);
# matches BOOTLOADER_FLASH_OFFSET in esptool's targets/ (h21 inherits h2's 0x0).
BOOTLOADER_OFFSETS = {
    "esp32": 0x1000,
    "esp32s2": 0x1000,
    "esp32c3": 0x0,
    "esp32s3": 0x0,
    "esp32c2": 0x0,
    "esp32c6": 0x0,
    "esp32h2": 0x0,
    "esp32c61": 0x0,
    "esp32p4": 0x2000,
    "esp32c5": 0x2000,
    "esp32h21": 0x0,
    "esp32h4": 0x2000,
}

# Partition types and subtypes (esp_partition.h).
TYPE_APP, TYPE_DATA = 0x00, 0x01
SUBTYPE_FACTORY, SUBTYPE_OTA_0 = 0x00, 0x10
SUBTYPE_SPIFFS, SUBTYPE_LITTLEFS = 0x82, 0x83
SUBTYPE_OTADATA = 0x00  # data partition subtype "ota"

# Kinds a flasher cannot do without: a null chip or offset for these fails the release.
REQUIRED_KINDS = ("app", "bootloader", "partitions")

# Staged suffix -> kind. '' is the app image. Order is the order assets are listed.
SUFFIXES = (
    ("-bootloader.bin", "bootloader"),
    ("-partitions.bin", "partitions"),
    (".bin", "app"),
    ("-littlefs.bin", "littlefs"),
    ("-spiffs.bin", "spiffs"),
    ("-boot_app0.bin", "otadata"),  # otadata image selecting ota_0; NVS untouched
    ("-factory.bin", "factory"),
    (".elf", "elf"),
)


def warn(msg: str) -> None:
    print(f"::warning::{msg}", file=sys.stderr)


def chip_from_image(data: bytes) -> str | None:
    """Read the chip out of an ESP app or bootloader image header."""
    if len(data) < 24 or data[0] != ESP_IMAGE_MAGIC:
        return None
    (chip_id,) = struct.unpack_from("<H", data, 12)
    return CHIP_IDS.get(chip_id)


def parse_partition_table(data: bytes) -> list[dict]:
    """Decode a binary ESP partition table into rows."""
    rows = []
    for i in range(0, len(data) - PARTITION_ENTRY + 1, PARTITION_ENTRY):
        entry = data[i:i + PARTITION_ENTRY]
        magic = entry[:2]
        if magic == PARTITION_MD5_MAGIC or magic == b"\xff\xff":
            break
        if magic != PARTITION_MAGIC:
            raise ValueError(f"bad partition entry magic {magic.hex()} at {i:#x}")
        ptype, subtype, offset, size = struct.unpack_from("<BBII", entry, 2)
        name = entry[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
        rows.append({"type": ptype, "subtype": subtype, "offset": offset,
                     "size": size, "label": name})
    return rows


def app_offset(rows: list[dict]) -> int | None:
    """Where the bootloader looks first: factory, else ota_0, else the first app."""
    apps = [r for r in rows if r["type"] == TYPE_APP]
    for subtype in (SUBTYPE_FACTORY, SUBTYPE_OTA_0):
        for r in apps:
            if r["subtype"] == subtype:
                return r["offset"]
    return apps[0]["offset"] if apps else None


def fs_offset(rows: list[dict], kind: str) -> int | None:
    """Data partition a filesystem image belongs in. LittleFS often reuses the spiffs subtype."""
    wanted = (SUBTYPE_LITTLEFS, SUBTYPE_SPIFFS) if kind == "littlefs" else (SUBTYPE_SPIFFS,)
    for subtype in wanted:
        for r in rows:
            if r["type"] == TYPE_DATA and r["subtype"] == subtype:
                return r["offset"]
    return None


def otadata_offset(rows: list[dict]) -> int | None:
    """The otadata partition (data, subtype ota), where boot_app0.bin goes."""
    for r in rows:
        if r["type"] == TYPE_DATA and r["subtype"] == SUBTYPE_OTADATA:
            return r["offset"]
    return None


def locate(factory: bytes, part: bytes, expected: int | None) -> int | None:
    """Find a part inside a factory image, trying the expected offset first."""
    if not part:
        return None
    if expected is not None and factory[expected:expected + len(part)] == part:
        return expected
    for off in range(0, len(factory) - len(part) + 1, FLASH_SECTOR):
        if factory[off:off + len(part)] == part:
            return off
    return None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def env_assets(dist: str, env: str, version: str) -> list[dict]:
    """Build the manifest entries for one environment's staged files."""
    files: dict[str, tuple[str, bytes]] = {}
    for suffix, kind in SUFFIXES:
        name = f"{env}-{version}{suffix}"
        path = os.path.join(dist, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                files[kind] = (name, fh.read())
    if not files:
        warn(f"{env}: no release assets found in {dist}")
        return []

    chip = None
    for kind in ("app", "bootloader", "factory"):
        if kind in files:
            data = files[kind][1]
            if kind == "factory":  # the bootloader sits somewhere in the first sectors
                for off in sorted(set(BOOTLOADER_OFFSETS.values())):
                    chip = chip_from_image(data[off:])
                    if chip:
                        break
            else:
                chip = chip_from_image(data)
            if chip:
                break
    if chip is None:
        warn(f"{env}: could not tell the chip from the image header")

    rows: list[dict] = []
    if "partitions" in files:
        try:
            rows = parse_partition_table(files["partitions"][1])
        except ValueError as exc:
            warn(f"{env}: {files['partitions'][0]}: {exc}")

    offsets: dict[str, int | None] = {
        "bootloader": BOOTLOADER_OFFSETS.get(chip) if chip else None,
        "partitions": DEFAULT_PARTITION_TABLE_OFFSET,
        "app": app_offset(rows) if rows else None,
        "littlefs": fs_offset(rows, "littlefs") if rows else None,
        "spiffs": fs_offset(rows, "spiffs") if rows else None,
        "otadata": otadata_offset(rows) if rows else None,
        "factory": 0x0,
        "elf": None,  # not flashed; for decoding backtraces and core dumps
    }

    # The factory image is the build system's own answer to "what goes where".
    # Only parts it contains can be verified; filesystem images never are.
    verified: dict[str, bool] = {}
    expected_bootloader = offsets["bootloader"]
    if "factory" in files:
        factory = files["factory"][1]
        for kind in ("bootloader", "partitions", "app"):
            if kind not in files:
                continue
            found = locate(factory, files[kind][1], offsets[kind])
            if found is None:
                warn(f"{env}: {kind} not found inside the factory image; keeping "
                     f"{_hex(offsets[kind])}")
            else:
                if found != offsets[kind]:
                    warn(f"{env}: {kind} sits at {found:#x} in the factory image, not "
                         f"{_hex(offsets[kind])}; using {found:#x}")
                    offsets[kind] = found
                verified[kind] = True
        # Arduino merges boot_app0 into the factory image; verify it when it is there,
        # without warning when it isn't.
        if "otadata" in files and offsets["otadata"] is not None:
            found = locate(factory, files["otadata"][1], offsets["otadata"])
            verified["otadata"] = found == offsets["otadata"]
        # The factory image starts at 0x0 if the bootloader is where the chip puts it.
        verified["factory"] = (verified.get("bootloader", False)
                               and expected_bootloader is not None
                               and offsets["bootloader"] == expected_bootloader)

    for kind in ("bootloader", "app", "littlefs", "spiffs", "otadata"):
        if kind in files and offsets[kind] is None:
            warn(f"{env}: no flash offset known for {files[kind][0]}")

    out = []
    for _, kind in SUFFIXES:
        if kind not in files:
            continue
        name, data = files[kind]
        out.append({
            "name": name,
            "env": env,
            "kind": kind,
            "chip": chip,
            "offset": offsets[kind],
            "size": len(data),
            "sha256": sha256(data),
            # True only when the offset was confirmed against the factory image.
            "verified": verified.get(kind, False),
        })
    return out


def problems(assets: list[dict]) -> list[str]:
    """Assets a flasher could not place: required kinds missing a chip or an offset."""
    out = []
    for a in assets:
        if a["kind"] not in REQUIRED_KINDS:
            continue
        missing = [f for f in ("chip", "offset") if a[f] is None]
        if missing:
            out.append(f"{a['name']}: no {' or '.join(missing)}")
    return out


def _hex(value: int | None) -> str:
    return "unknown" if value is None else f"{value:#x}"


def build_manifest(dist: str, envs: list[str], version: str, release_type: str = "",
                   repo: str = "", sha: str = "") -> dict:
    assets = []
    for env in envs:
        assets.extend(env_assets(dist, env, version))
    return {
        "schema": SCHEMA,
        "version": version,
        "release_type": release_type or None,
        "repo": repo or None,
        "commit": sha or None,
        "envs": sorted({a["env"] for a in assets}),
        "assets": assets,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dist", required=True, help="directory of staged release assets")
    ap.add_argument("--envs", required=True, help="JSON array of environments built")
    ap.add_argument("--version", required=True)
    ap.add_argument("--type", dest="release_type", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--sha", default="")
    ap.add_argument("--output", default="", help="defaults to <dist>/manifest.json")
    args = ap.parse_args(argv)

    try:
        envs = json.loads(args.envs)
    except json.JSONDecodeError:
        envs = None
    if not isinstance(envs, list) or not all(isinstance(e, str) for e in envs):
        print(f"::error::--envs must be a JSON array of strings, got {args.envs!r}")
        return 1

    manifest = build_manifest(args.dist, envs, args.version, args.release_type,
                              args.repo, args.sha)
    if not manifest["assets"]:
        print(f"::error::no assets for {envs} at {args.version} in {args.dist}")
        return 1
    bad = problems(manifest["assets"])
    for msg in bad:
        print(f"::error::{msg}")
    if bad:
        # Publishing these would hand a flasher a file it cannot place.
        return 1

    output = args.output or os.path.join(args.dist, "manifest.json")
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print(f"wrote {output}: {len(manifest['assets'])} assets across "
          f"{len(manifest['envs'])} envs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
