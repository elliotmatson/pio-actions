import json

from scripts.release_notes import (
    build_tokens,
    env_list,
    main,
    render,
    size_table,
)

MANIFEST = {
    "env": "esp32dev",
    "bin_bytes": 1048576,
    "flash_bytes": 1000000,
    "ram_bytes": 65536,
    "app_partition_bytes": 2097152,
    "partition_pct": 50.0,
}


def write_manifests(tmp_path, *manifests):
    for m in manifests:
        (tmp_path / f"{m['env']}-manifest.json").write_text(json.dumps(m))
    return str(tmp_path)


def test_tokens_are_substituted():
    out = render("{repo} {version} ({type})", build_tokens(
        "v1.2.3", "stable", "acme/fw", "abcdef1234", "", {}, today="2026-09-21"))
    assert out == "acme/fw v1.2.3 (stable)"


def test_unknown_braces_are_left_alone():
    # A template may legitimately contain braces -- a JSON example, a C macro,
    # a shell expansion in a flashing command. Only known tokens are touched.
    body = 'esptool --port ${PORT} write_flash 0x0 {version}.bin {"a": 1}'
    out = render(body, build_tokens("v1", "stable", "r", "s", "", {}))
    assert out == 'esptool --port ${PORT} write_flash 0x0 v1.bin {"a": 1}'


def test_short_sha_and_date():
    tokens = build_tokens("v1", "beta", "r", "0123456789abcdef", "", {},
                          today="2026-09-21")
    assert tokens["short_sha"] == "0123456"
    assert tokens["date"] == "2026-09-21"


def test_date_defaults_to_today():
    assert build_tokens("v1", "beta", "r", "s", "", {})["date"]


def test_size_table_lists_every_environment():
    table = size_table({
        "esp32dev": MANIFEST,
        "esp32s3": dict(MANIFEST, env="esp32s3", bin_bytes=2048, partition_pct=None,
                        app_partition_bytes=None),
    })
    assert "| `esp32dev` | 1.00 MB | 50.0% of 2.00 MB |" in table
    # No partition table found is reported, not guessed at.
    assert "| `esp32s3` | 2.0 KB | n/a |" in table


def test_size_table_is_empty_without_manifests():
    assert size_table({}) == ""


def test_envs_come_from_the_discover_output():
    assert env_list('["b", "a"]', {}) == "`b`, `a`"


def test_envs_fall_back_to_the_manifests():
    # A caller that does not pass the list should still get one rather than a
    # blank line in the middle of the release notes.
    assert env_list("", {"b": {}, "a": {}}) == "`a`, `b`"


def test_envs_survive_a_malformed_list():
    assert env_list("not json", {"a": {}}) == "`a`"


def test_main_renders_the_sizes_table(tmp_path):
    manifests = write_manifests(tmp_path, MANIFEST)
    template = tmp_path / "notes.md"
    template.write_text("## {version}\n\n{sizes}\n")
    out = tmp_path / "out.md"

    assert main(["--template", str(template), "--manifests", manifests,
                 "--version", "v9.9.9", "--output", str(out)]) == 0

    body = out.read_text()
    assert body.startswith("## v9.9.9")
    assert "| `esp32dev` |" in body


def test_main_fails_on_a_missing_template(tmp_path, capsys):
    assert main(["--template", str(tmp_path / "nope.md")]) == 1
    assert "not found" in capsys.readouterr().out


def test_an_empty_render_writes_nothing(tmp_path, capsys):
    # A template whose every line is a token that resolved to nothing would
    # otherwise hand GitHub a blank body and silently drop the generated notes.
    template = tmp_path / "notes.md"
    template.write_text("{sizes}\n")
    out = tmp_path / "out.md"

    assert main(["--template", str(template), "--output", str(out)]) == 0
    assert not out.exists()
    assert "rendered empty" in capsys.readouterr().out
