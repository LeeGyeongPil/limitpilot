# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-10

### Added

- First public release, continuing claude-account-pilot (the author's earlier personal
  project), with `limitpilot migrate` to move existing data.

### Known limitations

- `switch` does not yet warn when the target slot is over the configured cap (planned for the next version).
- With a Korean locale, a few labels (`needs login`, `slot N`, the sandbox summary line, `(active)`) stay in English; full translation is planned for the next version.
