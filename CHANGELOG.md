# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.3] - 2026-11-11

### Changed

- Re-release of 0.1.2 with no code changes. The 0.1.2 pull-request CI run was cancelled because a hosted macOS
  runner never picked up one matrix job; the same commit passed every job in its push run.

### Known limitations

- `migrate` still prints the English `nothing to migrate` line when there is no old data.
- Threshold messages may render as `임계치은(는)` because the particle is not chosen by the final consonant.
- With `LIMITPILOT_DEMO_ROOT` pointing at an empty folder, a traceback is shown instead of a friendly message.
- The `docs/images/05-doctor.png` screenshot still shows the older version number; the text output above is current.

## [0.1.2] - 2026-11-11

### Fixed

- With `--lang ko` (or a Korean locale), `doctor`, `migrate --dry-run` and `widget build/remove --dry-run`
  now print Korean instead of English. English output is unchanged.

### Known limitations

- Still English with `--lang ko`: the `nothing to migrate` line of `migrate --dry-run` (no legacy data), the real
  `widget install`/`uninstall` messages, and argparse's default text (`usage`, `options`, errors). README lists them.
- The `doctor` screenshot in the README was taken with an earlier version number.

## [0.1.1] - 2026-11-10

### Changed

- README: replace the comparison table with a short "when to use" section.

## [0.1.0] - 2026-11-10

### Added

- First public release, continuing claude-account-pilot (the author's earlier personal
  project), with `limitpilot migrate` to move existing data.

### Known limitations

- `switch` does not yet warn when the target slot is over the configured cap (planned for the next version).
- With a Korean locale, a few labels (`needs login`, `slot N`, the sandbox summary line, `(active)`) stay in English; full translation is planned for the next version.
