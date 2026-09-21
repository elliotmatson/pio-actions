import pytest

from scripts.size_report import (
    app_partition_bytes,
    classify_sections,
    parse_partition_csv,
    parse_size,
    render_markdown,
)

# Dual OTA, tab-separated, trailing commas, a comment header -- as written by hand.
DUAL_OTA = """# Name,\tType,\tSubType,\tOffset,\tSize,\tFlags
nvs,\tdata,\tnvs,\t0x9000,\t0x5000,\t
otadata,\tdata,\tota,\t0xe000,\t0x2000,\t
app0,\tapp,\tota_0,\t0x10000,\t0x1f0000,\t
app1,\tapp,\tota_1,\t0x200000,\t0x1f0000,   
coredump,\tdata,\tcoredump,\t0x3f0000,\t0x10000,
"""

# huge_app.csv ships with the framework: single app slot, K/M suffixes.
HUGE_APP = """# Name,   Type, SubType, Offset,  Size, Flags
nvs,      data, nvs,     0x9000,  0x5000,
otadata,  data, ota,     0xe000,  0x2000,
app0,     app,  factory, 0x10000, 3M,
spiffs,   data, spiffs,  0x310000,0xF0000,
"""


def section(name, size, *, alloc=True, write=False, exec_=False, progbits=True):
    return {"name": name, "size": size, "alloc": alloc, "write": write,
            "exec": exec_, "progbits": progbits}


def test_text_and_rodata_are_flash_only():
    got = classify_sections([
        section(".text", 1000, exec_=True),
        section(".rodata", 500),
    ])
    assert got["flash_bytes"] == 1500
    assert got["ram_bytes"] == 0


def test_bss_is_ram_only_because_it_ships_no_bytes():
    got = classify_sections([section(".bss", 4096, write=True, progbits=False)])
    assert got["ram_bytes"] == 4096
    assert got["flash_bytes"] == 0


def test_data_counts_against_both_budgets():
    got = classify_sections([section(".data", 256, write=True)])
    assert got["flash_bytes"] == 256
    assert got["ram_bytes"] == 256


def test_non_allocated_sections_are_ignored():
    got = classify_sections([
        section(".debug_info", 900_000, alloc=False),
        section(".comment", 1024, alloc=False),
    ])
    assert got["flash_bytes"] == 0
    assert got["ram_bytes"] == 0
    assert got["sections"] == {}


def test_esp32_dram_and_iram_land_in_the_right_budgets():
    got = classify_sections([
        section(".iram0.text", 60_000, exec_=True, write=True),
        section(".dram0.bss", 20_000, write=True, progbits=False),
        section(".flash.rodata", 300_000),
    ])
    assert got["ram_bytes"] == 80_000
    assert got["flash_bytes"] == 360_000


def test_zero_length_sections_do_not_clutter_the_breakdown():
    got = classify_sections([section(".empty", 0), section(".text", 10, exec_=True)])
    assert list(got["sections"]) == [".text"]


@pytest.mark.parametrize("raw,expected", [
    ("0x1f0000", 0x1F0000),
    ("1M", 1024 * 1024),
    ("3M", 3 * 1024 * 1024),
    ("1500K", 1500 * 1024),
    ("65536", 65536),
    (" 0x5000 ", 0x5000),
])
def test_partition_sizes_parse_in_every_notation(raw, expected):
    assert parse_size(raw) == expected


def test_parses_a_tab_separated_table():
    rows = parse_partition_csv(DUAL_OTA)
    assert [r["name"] for r in rows] == ["nvs", "otadata", "app0", "app1", "coredump"]


def test_ota_ceiling_is_the_smaller_slot_not_their_sum():
    rows = parse_partition_csv(DUAL_OTA)
    assert app_partition_bytes(rows) == 0x1F0000


def test_single_slot_table_with_suffix_size():
    rows = parse_partition_csv(HUGE_APP)
    assert app_partition_bytes(rows) == 3 * 1024 * 1024


def test_table_without_app_partition_reports_no_ceiling():
    rows = parse_partition_csv("nvs, data, nvs, 0x9000, 0x5000,\n")
    assert app_partition_bytes(rows) is None


def test_markdown_states_the_percentage_when_a_ceiling_is_known():
    line = render_markdown({
        "env": "app", "bin_bytes": 1_048_576, "flash_bytes": 1_000_000,
        "ram_bytes": 65_536, "app_partition_bytes": 0x1F0000, "partition_pct": 51.2,
    })
    assert "app" in line and "51.2%" in line


def test_markdown_degrades_when_no_partition_table_was_found():
    line = render_markdown({
        "env": "esp32", "bin_bytes": 900_000, "flash_bytes": 880_000,
        "ram_bytes": 40_000, "app_partition_bytes": None, "partition_pct": None,
    })
    assert "no partition table found" in line


# --- manifest assembly -------------------------------------------------------

def _fake_project(tmp_path, ini_body, bin_bytes=200_000, partitions=DUAL_OTA):
    project = tmp_path / "proj"
    build = project / ".pio" / "build" / "esp32dev"
    build.mkdir(parents=True)
    (build / "firmware.bin").write_bytes(b"\x00" * bin_bytes)
    (build / "bootloader.bin").write_bytes(b"\x01" * 1024)
    (project / "platformio.ini").write_text(ini_body)
    if partitions:
        (project / "partitions.csv").write_text(partitions)
    return project, build


def test_manifest_measures_image_and_partition_without_an_elf(tmp_path):
    from scripts.size_report import build_manifest

    project, build = _fake_project(
        tmp_path, "[env:esp32dev]\nboard_build.partitions = partitions.csv\n")
    m = build_manifest(str(build), "esp32dev", str(project))

    assert m["bin_bytes"] == 200_000
    assert m["app_partition_bytes"] == 0x1F0000
    assert m["partition_pct"] == round(100 * 200_000 / 0x1F0000, 2)
    # No ELF present: the section breakdown degrades instead of failing.
    assert m["flash_bytes"] is None and m["sections"] == {}


def test_manifest_hashes_every_image_it_finds(tmp_path):
    from scripts.size_report import build_manifest

    project, build = _fake_project(
        tmp_path, "[env:esp32dev]\nboard_build.partitions = partitions.csv\n")
    m = build_manifest(str(build), "esp32dev", str(project))

    assert set(m["artifacts"]) == {"firmware.bin", "bootloader.bin"}
    assert all(len(a["sha256"]) == 64 for a in m["artifacts"].values())
    assert m["artifacts"]["bootloader.bin"]["bytes"] == 1024


def test_partitions_setting_is_inherited_from_the_base_env_section(tmp_path):
    from scripts.size_report import build_manifest

    # Projects commonly set board_build.partitions on [env], not per env.
    project, build = _fake_project(
        tmp_path, "[env]\nboard_build.partitions = partitions.csv\n\n[env:esp32dev]\nboard = esp32dev\n")
    m = build_manifest(str(build), "esp32dev", str(project))

    assert m["app_partition_bytes"] == 0x1F0000


def test_missing_partition_table_leaves_the_percentage_unknown(tmp_path):
    from scripts.size_report import build_manifest

    project, build = _fake_project(
        tmp_path, "[env:esp32dev]\nboard = esp32dev\n", partitions=None)
    m = build_manifest(str(build), "esp32dev", str(project))

    assert m["bin_bytes"] == 200_000
    assert m["app_partition_bytes"] is None
    assert m["partition_pct"] is None


def test_cli_creates_the_manifest_directory_it_was_pointed_at(tmp_path):
    # The size step runs before the staging step makes dist/, so the script has
    # to create it rather than assume it.
    import json
    import subprocess
    import sys

    project, build = _fake_project(
        tmp_path, "[env:esp32dev]\nboard_build.partitions = partitions.csv\n")
    out = project / "dist" / "esp32dev-manifest.json"

    subprocess.run(
        [sys.executable, "scripts/size_report.py",
         "--build-dir", str(build), "--env", "esp32dev",
         "--project-dir", str(project), "--output", str(out)],
        check=True, capture_output=True,
    )

    assert json.loads(out.read_text())["env"] == "esp32dev"


# --- extends chains ----------------------------------------------------------

EXTENDS_INI = """
[arduino_base]
framework = arduino
board_build.partitions = partitions.csv

[env:esp32dev]
extends = arduino_base
board = esp32dev

[env:native]
platform = native
"""


def test_partitions_resolve_through_an_extends_chain(tmp_path):
    from scripts.size_report import partitions_spec_for_env

    (tmp_path / "platformio.ini").write_text(EXTENDS_INI)
    assert partitions_spec_for_env(str(tmp_path), "esp32dev") == "partitions.csv"


def test_an_env_outside_the_chain_inherits_nothing_from_it(tmp_path):
    from scripts.size_report import partitions_spec_for_env

    # native does not extend arduino_base, so it must not pick up its table.
    (tmp_path / "platformio.ini").write_text(EXTENDS_INI)
    assert partitions_spec_for_env(str(tmp_path), "native") == ""


def test_an_envs_own_setting_beats_the_section_it_extends(tmp_path):
    from scripts.size_report import partitions_spec_for_env

    (tmp_path / "platformio.ini").write_text(
        EXTENDS_INI + "\nboard_build.partitions = huge_app.csv\n")
    assert partitions_spec_for_env(str(tmp_path), "native") == "huge_app.csv"


def test_a_multi_level_chain_is_followed_to_the_end(tmp_path):
    from scripts.size_report import partitions_spec_for_env

    (tmp_path / "platformio.ini").write_text("""
[root]
board_build.partitions = deep.csv

[middle]
extends = root

[env:leaf]
extends = middle
""")
    assert partitions_spec_for_env(str(tmp_path), "leaf") == "deep.csv"


def test_a_circular_extends_terminates_instead_of_recursing_forever(tmp_path):
    from scripts.size_report import partitions_spec_for_env

    (tmp_path / "platformio.ini").write_text("""
[a]
extends = b

[b]
extends = a

[env:loop]
extends = a
""")
    assert partitions_spec_for_env(str(tmp_path), "loop") == ""
