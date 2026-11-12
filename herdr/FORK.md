# This is a fork

This folder is [herdrdev/herdr](https://github.com/herdrdev/herdr), imported with
`git subtree` at tag `v0.9.5` and built and released as part of
[agent-picker](https://github.com/matthewstar05/agent-picker). It is licensed
under the Apache License 2.0 (see `LICENSE`) and is not an official Herdr
release. Report problems with these builds to agent-picker, not to Herdr.

## Changes from upstream

- `src/update.rs` and `src/remote/attach.rs` read the update manifest from
  agent-picker's releases (`herdr-latest.json`) instead of herdr.dev, so
  `herdr update` never replaces this build with an upstream one. The whole
  install is updated with `agent --picker-update`.

List every further change here, as the license asks.

## Building

From the repository root, `python3 scripts/herdr_bin.py build` runs
`cargo build --release --locked` here and copies the result to `bin/herdr`,
where `agent` looks for it. Releases are built by the root
`.github/workflows/release.yml`; the workflows under `herdr/.github` are
upstream's and do not run in this repository.

## Taking a newer upstream version

From the repository root, with a clean working tree:

```sh
git subtree pull --prefix=herdr https://github.com/herdrdev/herdr <tag> --squash
```

Resolve conflicts in the files listed above, then build. Push the result with
a plain `git push`; do not rebase a subtree merge, which would replay Herdr's
files at the repository root.
