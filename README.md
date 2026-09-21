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

Grant `contents: write` on the calling job if it will ever produce a release;
the release job inherits that rather than requesting it, so a build-only caller
can stay on `contents: read`.

## What it does differently

**One job per environment.** Environments come from `platformio.ini`, so adding
an env to a project adds a CI job with no workflow edit. `default` mirrors a
bare `pio run` (`default_envs` when set, every env otherwise); `all` forces
everything. `fail-fast: false`, so one board's failure doesn't hide the rest.

Prefer `default` unless you know the project has no host-side environments.
A project with an `[env:native]` deliberately keeps it out of `default_envs`,
because a bare `pio run` walks into it and fails on `<Arduino.h>`; `all` would
reintroduce that.

**Artifacts are artifacts.** The per-repo versions passed `.bin` files from the
build job to the release job through `actions/cache` keyed on `github.run_id`.
Cache entries can be evicted and carry no retention policy. This uses
`upload-artifact`/`download-artifact`.

**The cache key invalidates.** The old key was `${{ runner.os }}-pio`, constant
forever, so bumping a platform silently reused the toolchain cache built for
the previous one. Keys now hash `platformio.ini`, with `restore-keys` for a
warm start on a miss.

**The merged image ships too.** pioarduino writes a `firmware.factory.bin`
alongside the app image — bootloader, partition table and application already
combined at their flash offsets. The per-repo workflows left it in the build
directory, so flashing a board from a release meant three files and three
offsets. It is now published as `<env>-<version>-factory.bin`.

**Pull requests get a size diff.** On a `pull_request`, the workflow also
builds the base commit and posts a comment with the flash, RAM and image delta
per environment, the app-partition percentage before and after, and a collapsed
per-section breakdown of what moved. The comment is updated in place rather than
appended, and the baseline is cached against the base commit so it is built once
per pull request rather than on every push to the branch.

The baseline is the **merge base** of the pull request, not the base branch
tip. `base.sha` follows the base branch as it moves, which would quietly fold
other people's commits into your diff.

The baseline is built with the *head* version string on purpose: a different
`FW_VERSION` is a different number of bytes of `.rodata`, and that noise would
otherwise appear in the diff as though the change had caused it.

Sections say a change cost 4 KB of `.flash.text`. The comment also carries a
per-symbol table saying *which function* it was, demangled, taken from the ELF
symbol table — no extra tooling needed.

Every sized symbol is recorded, not the largest N. Ranking by size and cutting
sounds thrifty, but an ESP32 image's biggest symbols are newlib and FreeRTOS
internals: capping at 500 on a blink sketch put the boundary at 100 bytes and
hid every function in the sketch, which is exactly what a reviewer needs to
see. The full table costs a couple of hundred kilobytes of JSON.

Growth alone never fails the job — reviewers should see a delta, not be blocked
by one. The job fails only when an image reaches `size-fail-pct` of its app
partition (default 95%) or can no longer flash at all.

Grant the calling job `pull-requests: write` for the comment. Without it the
diff still lands in the job summary and the step warns. A pull request from a
fork cannot be commented on from the `pull_request` event at all; that needs the
`workflow_run` pattern, which is not built yet.

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
| `scripts/size_diff.py` | Renders the pull-request size comment |
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

`examples/blink` is shaped like the repos this serves rather than minimized:
three board envs so the matrix fans out, one of them building Arduino as an
ESP-IDF component the way the real firmware does, a host env for unit tests, a
dual-OTA partition table, and blink timing extracted into `lib/blink` so the
same code compiles for the firmware and for the host test runner. The rollover test is the
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
5. Testing — `pio test -e native`, then on-target. `examples/blink` already
   shows the shape; the reusable workflow should generalize it.
