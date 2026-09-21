import pytest

from scripts.size_report import (
    app_partition_bytes,
    classify_sections,
    parse_partition_csv,
    parse_size,
    render_markdown,
)

# hub/partitions.csv: dual OTA, tab-separated, trailing commas, a comment header.
HUB_PARTITIONS = """# Name,\tType,\tSubType,\tOffset,\tSize,\tFlags
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


def test_parses_hubs_tab_separated_table():
    rows = parse_partition_csv(HUB_PARTITIONS)
    assert [r["name"] for r in rows] == ["nvs", "otadata", "app0", "app1", "coredump"]


def test_ota_ceiling_is_the_smaller_slot_not_their_sum():
    rows = parse_partition_csv(HUB_PARTITIONS)
    assert app_partition_bytes(rows) == 0x1F0000


def test_single_slot_table_with_suffix_size():
    rows = parse_partition_csv(HUGE_APP)
    assert app_partition_bytes(rows) == 3 * 1024 * 1024


def test_table_without_app_partition_reports_no_ceiling():
    rows = parse_partition_csv("nvs, data, nvs, 0x9000, 0x5000,\n")
    assert app_partition_bytes(rows) is None


def test_markdown_states_the_percentage_when_a_ceiling_is_known():
    line = render_markdown({
        "env": "hub", "bin_bytes": 1_048_576, "flash_bytes": 1_000_000,
        "ram_bytes": 65_536, "app_partition_bytes": 0x1F0000, "partition_pct": 51.2,
    })
    assert "hub" in line and "51.2%" in line


def test_markdown_degrades_when_no_partition_table_was_found():
    line = render_markdown({
        "env": "esp32", "bin_bytes": 900_000, "flash_bytes": 880_000,
        "ram_bytes": 40_000, "app_partition_bytes": None, "partition_pct": None,
    })
    assert "no partition table found" in line
