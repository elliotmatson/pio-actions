import pytest

from scripts.list_envs import parse_ini, select

TWO_DEFAULTS = """
[platformio]
default_envs =
    app
    app-debug
name = Example Firmware

[env]
platform = https://example.invalid/platform.zip
board_build.partitions = partitions.csv

[env:app]
build_flags = -DROLE_HUB

[env:app-debug]
build_flags = -DROLE_HUB -DDEBUG
"""

# A project can declare more envs than it defaults to, so a bare `pio run`
# builds only the subset.
SUBSET_DEFAULT = """
[platformio]
default_envs = clx-dp02

[env]
monitor_speed = 115200

[env:hp_dev]
board = clx-dp02

[env:clx-dp02]
board = clx-dp02
"""

NO_DEFAULTS = """
[env:esp32cam]
board = esp32cam

[env:esp_eye]
board = esp32dev
"""

# Projects interpolate ${env.lib_deps}; a raw parser must not choke on it.
INTERPOLATED = """
[env]
lib_deps = bblanchon/ArduinoJson@^7.4.2

[env:esp32]
lib_deps =
  ESP32Async/AsyncTCP@^3.4.5
  ${env.lib_deps}
"""


def test_reads_multiline_default_envs():
    envs, defaults = parse_ini(TWO_DEFAULTS)
    assert envs == ["app", "app-debug"]
    assert defaults == ["app", "app-debug"]


def test_base_env_section_is_not_an_environment():
    envs, _ = parse_ini(TWO_DEFAULTS)
    assert "" not in envs and "env" not in envs


def test_default_selection_honours_default_envs():
    envs, defaults = parse_ini(SUBSET_DEFAULT)
    assert select(envs, defaults, "default") == ["clx-dp02"]


def test_all_selection_covers_every_env():
    envs, defaults = parse_ini(SUBSET_DEFAULT)
    assert sorted(select(envs, defaults, "all")) == ["clx-dp02", "hp_dev"]


def test_default_falls_back_to_every_env_when_unset():
    envs, defaults = parse_ini(NO_DEFAULTS)
    assert defaults == []
    assert select(envs, defaults, "default") == ["esp32cam", "esp_eye"]


def test_explicit_list_accepts_commas_and_whitespace():
    envs, defaults = parse_ini(TWO_DEFAULTS)
    assert select(envs, defaults, "app, app-debug") == ["app", "app-debug"]


def test_typo_fails_loudly_rather_than_building_nothing():
    envs, defaults = parse_ini(TWO_DEFAULTS)
    with pytest.raises(SystemExit) as err:
        select(envs, defaults, "appp")
    assert "appp" in str(err.value)


def test_interpolation_does_not_break_parsing():
    envs, _ = parse_ini(INTERPOLATED)
    assert envs == ["esp32"]
