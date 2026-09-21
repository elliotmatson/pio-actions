## Firmware {version}

Built {date} from `{short_sha}` for {envs}.

### Flashing

Each environment ships two images:

- `<env>-{version}.bin` — the application image, for OTA and for
  `esptool.py write_flash 0x10000`.
- `<env>-{version}-factory.bin` — bootloader, partition table and application
  merged into one file, for a blank board:

```
esptool.py --chip auto write_flash 0x0 <env>-{version}-factory.bin
```

Verify a download against `SHA256SUMS` before flashing it.

### Sizes

{sizes}
