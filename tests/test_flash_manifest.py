import hashlib
import json
import struct

import pytest

from scripts.flash_manifest import (
    app_offset,
    build_manifest,
    chip_from_image,
    main,
    parse_partition_table,
    problems,
)
from scripts.size_diff import load_manifests

CHIP_ESP32, CHIP_S2, CHIP_S3, CHIP_P4 = 0x0000, 0x0002, 0x0009, 0x0012


def image(chip_id: int, body: bytes = b"") -> bytes:
    """An ESP image header (magic, segments, mode, size/freq, entry, ext header) plus a body."""
    header = struct.pack("<BBBBI", 0xE9, 1, 2, 0x2F, 0x40080000)
    # wp_pin, spi_pin_drv[3], chip_id, then the rest of the 16-byte extended header.
    ext = struct.pack("<B3sH", 0xEE, b"\0\0\0", chip_id) + b"\0" * 10
    return header + ext + body


def entry(ptype, subtype, offset, size, label):
    return (b"\xaa\x50" + struct.pack("<BBII", ptype, subtype, offset, size)
            + label.encode().ljust(16, b"\0") + b"\0" * 4)


def table(*rows) -> bytes:
    data = b"".join(entry(*r) for r in rows)
    data += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(data).digest()
    return data.ljust(0xC00, b"\xff")


OTA_TABLE = table(
    (1, 0x02, 0x9000, 0x5000, "nvs"),
    (1, 0x00, 0xE000, 0x2000, "otadata"),
    (0, 0x10, 0x10000, 0x140000, "app0"),
    (0, 0x11, 0x150000, 0x140000, "app1"),
    (1, 0x82, 0x290000, 0x160000, "spiffs"),
)


def factory(parts: dict[int, bytes]) -> bytes:
    end = max(off + len(p) for off, p in parts.items())
    buf = bytearray(b"\xff" * end)
    for off, p in parts.items():
        buf[off:off + len(p)] = p
    return bytes(buf)


def stage(tmp_path, env, version, **files):
    names = {"app": f"{env}-{version}.bin", "elf": f"{env}-{version}.elf"}
    for kind, data in files.items():
        name = names.get(kind, f"{env}-{version}-{kind}.bin")
        (tmp_path / name).write_bytes(data)


def by_kind(manifest, env):
    return {a["kind"]: a for a in manifest["assets"] if a["env"] == env}


def test_chip_is_read_from_the_image_header():
    assert chip_from_image(image(CHIP_ESP32)) == "esp32"
    assert chip_from_image(image(CHIP_S3)) == "esp32s3"
    assert chip_from_image(b"\x00" * 32) is None
    assert chip_from_image(image(0x7777)) is None


def test_partition_table_round_trips():
    rows = parse_partition_table(OTA_TABLE)
    assert [r["label"] for r in rows] == ["nvs", "otadata", "app0", "app1", "spiffs"]
    assert rows[2]["offset"] == 0x10000


def test_bad_partition_magic_is_an_error():
    with pytest.raises(ValueError):
        parse_partition_table(b"\x12\x34" + b"\0" * 30)


def test_factory_app_partition_wins_over_ota():
    rows = parse_partition_table(table(
        (0, 0x10, 0x20000, 0x100000, "ota_0"),
        (0, 0x00, 0x10000, 0x10000, "factory"),
    ))
    assert app_offset(rows) == 0x10000


def test_esp32_release_with_factory_and_elf(tmp_path):
    boot, app = image(CHIP_ESP32, b"boot" * 100), image(CHIP_ESP32, b"app" * 1000)
    stage(tmp_path, "hub", "v1.0.0", app=app, bootloader=boot, partitions=OTA_TABLE,
          factory=factory({0x1000: boot, 0x8000: OTA_TABLE, 0x10000: app}),
          elf=b"\x7fELF fake")
    m = build_manifest(str(tmp_path), ["hub"], "v1.0.0", "stable", "acme/hub", "abc")
    assert m["schema"] == 1 and m["version"] == "v1.0.0" and m["envs"] == ["hub"]
    kinds = by_kind(m, "hub")
    assert {k: v["offset"] for k, v in kinds.items()} == {
        "bootloader": 0x1000, "partitions": 0x8000, "app": 0x10000,
        "factory": 0x0, "elf": None,
    }
    assert all(a["chip"] == "esp32" for a in m["assets"])
    assert kinds["app"]["sha256"] == hashlib.sha256(app).hexdigest()
    assert kinds["app"]["size"] == len(app)
    assert kinds["elf"]["name"] == "hub-v1.0.0.elf"
    assert {k: v["verified"] for k, v in kinds.items()} == {
        "bootloader": True, "partitions": True, "app": True,
        "factory": True, "elf": False,
    }
    assert problems(m["assets"]) == []


def test_s3_bootloader_goes_at_zero(tmp_path):
    stage(tmp_path, "s3", "v2", app=image(CHIP_S3), bootloader=image(CHIP_S3),
          partitions=OTA_TABLE)
    kinds = by_kind(build_manifest(str(tmp_path), ["s3"], "v2"), "s3")
    assert kinds["bootloader"]["offset"] == 0x0
    assert kinds["app"]["offset"] == 0x10000


def test_p4_bootloader_offset(tmp_path):
    stage(tmp_path, "p4", "v2", app=image(CHIP_P4), bootloader=image(CHIP_P4))
    assert by_kind(build_manifest(str(tmp_path), ["p4"], "v2"), "p4")["bootloader"]["offset"] == 0x2000


def test_factory_image_overrides_a_moved_partition_table(tmp_path, capsys):
    # CONFIG_PARTITION_TABLE_OFFSET moved the table; only the factory image knows.
    boot, app = image(CHIP_S2, b"b" * 64), image(CHIP_S2, b"a" * 64)
    stage(tmp_path, "s2", "v3", app=app, bootloader=boot, partitions=OTA_TABLE,
          factory=factory({0x1000: boot, 0xA000: OTA_TABLE, 0x10000: app}))
    kinds = by_kind(build_manifest(str(tmp_path), ["s2"], "v3"), "s2")
    assert kinds["partitions"]["offset"] == 0xA000
    assert "partitions sits at 0xa000" in capsys.readouterr().err


def test_filesystem_image_is_placed_in_its_partition(tmp_path):
    stage(tmp_path, "fs", "v1", app=image(CHIP_ESP32), partitions=OTA_TABLE,
          littlefs=b"\0" * 64)
    assert by_kind(build_manifest(str(tmp_path), ["fs"], "v1"), "fs")["littlefs"]["offset"] == 0x290000


def test_without_a_partition_table_the_app_offset_is_unknown(tmp_path, capsys):
    stage(tmp_path, "bare", "v1", app=image(CHIP_ESP32))
    kinds = by_kind(build_manifest(str(tmp_path), ["bare"], "v1"), "bare")
    assert kinds["app"]["offset"] is None
    assert "no flash offset known" in capsys.readouterr().err


def test_hyphenated_envs_do_not_capture_each_other(tmp_path):
    stage(tmp_path, "clx-dp02", "v5.0.0-b5", app=image(CHIP_S2), bootloader=image(CHIP_S2))
    stage(tmp_path, "clx-dp02-lite", "v5.0.0-b5", app=image(CHIP_S2))
    m = build_manifest(str(tmp_path), ["clx-dp02", "clx-dp02-lite"], "v5.0.0-b5")
    assert set(by_kind(m, "clx-dp02")) == {"app", "bootloader"}
    assert set(by_kind(m, "clx-dp02-lite")) == {"app"}


def test_main_writes_manifest_json_that_size_tools_ignore(tmp_path):
    stage(tmp_path, "hub", "v1", app=image(CHIP_ESP32), partitions=OTA_TABLE)
    # The size manifest the build job also stages into dist.
    (tmp_path / "hub-manifest.json").write_text(json.dumps({"env": "hub", "bin_bytes": 1}))
    assert main(["--dist", str(tmp_path), "--envs", '["hub"]', "--version", "v1",
                 "--type", "beta"]) == 0
    written = json.loads((tmp_path / "manifest.json").read_text())
    assert written["release_type"] == "beta"
    assert load_manifests(str(tmp_path)) == {"hub": {"env": "hub", "bin_bytes": 1}}


def test_main_fails_when_nothing_matches(tmp_path):
    assert main(["--dist", str(tmp_path), "--envs", '["hub"]', "--version", "v1"]) == 1


def test_without_a_factory_image_nothing_is_verified(tmp_path):
    stage(tmp_path, "s3", "v2", app=image(CHIP_S3), bootloader=image(CHIP_S3),
          partitions=OTA_TABLE, littlefs=b"\0" * 8)
    kinds = by_kind(build_manifest(str(tmp_path), ["s3"], "v2"), "s3")
    assert not any(a["verified"] for a in kinds.values())


@pytest.mark.parametrize("chip_id,chip,offset", [(25, "esp32h21", 0x0), (28, "esp32h4", 0x2000)])
def test_newer_chips(tmp_path, chip_id, chip, offset):
    stage(tmp_path, "n", "v1", bootloader=image(chip_id))
    boot = by_kind(build_manifest(str(tmp_path), ["n"], "v1"), "n")["bootloader"]
    assert (boot["chip"], boot["offset"]) == (chip, offset)


def test_unknown_chip_fails_the_release(tmp_path, capsys):
    stage(tmp_path, "x", "v1", app=image(0x7777), bootloader=image(0x7777),
          partitions=OTA_TABLE)
    assert main(["--dist", str(tmp_path), "--envs", '["x"]', "--version", "v1"]) == 1
    out = capsys.readouterr().out
    assert "::error::x-v1-bootloader.bin: no chip or offset" in out
    assert "::error::x-v1.bin: no chip" in out


def test_corrupt_partition_table_fails_the_release(tmp_path, capsys):
    stage(tmp_path, "c", "v1", app=image(CHIP_ESP32), partitions=b"\x12\x34" + b"\0" * 62)
    assert main(["--dist", str(tmp_path), "--envs", '["c"]', "--version", "v1"]) == 1
    assert "::error::c-v1.bin: no offset" in capsys.readouterr().out
    assert not (tmp_path / "manifest.json").exists()


def test_a_null_elf_offset_is_fine(tmp_path):
    stage(tmp_path, "e", "v1", app=image(CHIP_ESP32), partitions=OTA_TABLE, elf=b"\x7fELF")
    assert main(["--dist", str(tmp_path), "--envs", '["e"]', "--version", "v1"]) == 0


@pytest.mark.parametrize("envs", ["not json", '{"a": 1}', "[1, 2]", ""])
def test_malformed_envs_is_an_error_line_not_a_traceback(tmp_path, capsys, envs):
    assert main(["--dist", str(tmp_path), "--envs", envs, "--version", "v1"]) == 1
    assert capsys.readouterr().out.startswith("::error::--envs must be a JSON array")
