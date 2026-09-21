# pio-actions

Shared GitHub Actions for PlatformIO / pioarduino firmware repos.

These replace the `build-release.yml` that had been copy-pasted into seven
repos and drifted into four different versions of itself — different Python
versions, different `checkout` majors, different release actions, and a
PlatformIO pin documented by the same eight-line comment pasted twice.

## Using it

```yaml
jobs:
  firmware:
    uses: elliotmatson/pio-actions/.github/workflows/build-release.yml@v1
    permissions:
      contents: write
    with:
      release-type: ${{ inputs.release-type || 'build' }}
      release-version: ${{ inputs.release-version || '' }}
    secrets:
      gh-token: ${{ secrets.GH_PAT }}
```

See [`examples/`](examples/) for a complete caller.

## What it does differently

**One job per environment.** Environments come from `platformio.ini`, so adding
an env to a project adds a CI job with no workflow edit. `default` mirrors a
bare `pio run` (`default_envs` when set, every env otherwise); `all` forces
everything. `fail-fast: false`, so one board's failure doesn't hide the rest.

Prefer `default` unless you know the project has no host-side environments.
lp-p2p keeps `[env:native]` out of `default_envs` precisely because a bare
`pio run` walks into it and fails on `<Arduino.h>`; `all` would reintroduce
that.

**Artifacts are artifacts.** The per-repo versions passed `.bin` files from the
build job to the release job through `actions/cache` keyed on `github.run_id`.
Cache entries can be evicted and carry no retention policy. This uses
`upload-artifact`/`download-artifact`.

**The cache key invalidates.** The old key was `${{ runner.os }}-pio`, constant
forever, so bumping a platform silently reused the toolchain cache built for
the previous one. Keys now hash `platformio.ini`, with `restore-keys` for a
warm start on a miss.

**Every build is measured.** Each environment emits a manifest with image size,
flash/RAM section totals, and the percentage of the app partition consumed —
read from the project's real partition table, including framework-shipped ones
like `huge_app.csv`. This is what the memory-diff workflow will diff.

## Components

| Path | Purpose |
| --- | --- |
| `.github/workflows/build-release.yml` | Reusable build + release workflow |
| `actions/setup-pio` | Pinned PlatformIO, invalidating cache, private-lib git auth |
| `actions/list-envs` | Environment discovery for the build matrix |
| `actions/firmware-size` | Size manifest for one built environment |
| `scripts/` | The Python behind the actions, unit-tested in `tests/` |
| `examples/blink` | Fixture firmware the workflows are tested against |
| `.github/workflows/tests.yml` | Everything above, run on every push and PR |

### The PlatformIO pin

`setup-pio` defaults to `platformio==6.1.19`, not `--upgrade`. PlatformIO 6.2.0
requires `tool-scons ~4.41101.0` and installs it over the SCons 4.8.1 that
pioarduino pins, which fails the build with `No module named
'SCons.Tool.FortranCommon'`. Floating the version meant CI broke with no change
to the repo. It now lives in one place; bump it here when pioarduino and
PlatformIO core agree on a newer SCons.

## How sizes are counted

Two numbers, because they answer different questions:

- **Image bytes** — `firmware.bin` on disk, including the image header, segment
  headers, padding and checksum. This is what has to fit the partition, so it
  drives the percentage.
- **Flash / RAM bytes** — from the ELF section table. RAM is every allocated
  writable section (`.data`, `.bss`, `.dram0.*`); flash is every allocated
  section carrying initialized content. `.data` counts against both, since it
  ships in the image and occupies RAM at runtime. This matches how PlatformIO's
  own size report draws the line.

For a dual-OTA table the ceiling is the **smaller** app slot, not the sum — an
update has to fit whichever slot it lands in.

## Testing this repo

`tests.yml` runs the lot:

| Job | Covers |
| --- | --- |
| `scripts` | pytest over `scripts/` |
| `lint` | actionlint over every workflow |
| `native` | `pio test -e native` in `examples/blink` |
| `firmware` | a real two-env build driven through `build-release.yml` |
| `verify` | asserts the size manifests describe a plausible build |

`examples/blink` is shaped like the repos this serves rather than minimized: two
board envs so the matrix fans out, a host env for unit tests, a dual-OTA
partition table, and blink timing extracted into `lib/blink` so the same code
compiles for the firmware and for the host test runner. The rollover test is the
reason that split earns its keep — a naive `now > last + interval` stalls for
49.7 days after `millis()` wraps.

`verify` checks real numbers, not just exit codes: it asserts the app partition
resolves to `examples/blink/partitions.csv`'s 0x1E0000 slot, so a regression in
partition parsing fails the build instead of quietly reporting `n/a`.

### Testing the actions themselves

`build-release.yml` takes an `actions-ref`, defaulting to `v1`. The repo's own
run passes `${{ github.sha }}`, so a pull request exercises the actions it
changes rather than the last release. Consumers leave it alone.

## Versioning

Consumers pin `@v1`. Releases are `v1.x.y` with `v1` moved to the newest.

## Roadmap

1. ~~`setup-pio` + `build-release`, with `examples/blink` as a test fixture~~
2. `memory-diff` — flash/RAM delta as a sticky PR comment, against a cached
   merge-base build
3. `static-analysis` — `pio check` → SARIF → inline PR annotations
4. `pio-update` — dependency bumps for registry libs, git-tagged libs and
   pioarduino platform URLs, which Dependabot and Renovate don't cover
5. Testing — `pio test -e native`, then on-target. Not greenfield: lp-p2p
   already has `[env:native]` and real suites under `test/`, so the reusable
   workflow should generalize that rather than invent a convention.
