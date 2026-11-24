## Firmware {version}

Built {date} from `{short_sha}` for {envs}.

### Flashing

- `<env>-{version}.bin` — the application image, for OTA.
- `<env>-{version}-factory.bin` — bootloader, partition table, boot-slot
  selection (otadata) and application merged into one file, **for a blank
  board only**: it pads the gaps with 0xFF, NVS included, so on a board in use
  it erases the settings.

```
esptool.py --chip auto write_flash 0x0 <env>-{version}-factory.bin
```

To reflash a board in use without touching its settings, write the pieces
separately, including `-boot_app0.bin` so the board boots the app just written
(offsets for the default ESP32 partition table):

```
esptool.py --chip esp32 write_flash 0x1000 <env>-{version}-bootloader.bin \
  0x8000 <env>-{version}-partitions.bin 0xe000 <env>-{version}-boot_app0.bin \
  0x10000 <env>-{version}.bin
```

Verify a download against `SHA256SUMS` before flashing it.

### Sizes

{sizes}
