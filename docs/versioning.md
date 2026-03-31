# Versioning

This repo should use an explicit repo version as the install and migration key.

## Source Of Truth

The current desired server version lives in [`VERSION`](../VERSION).

Format:

```text
MAJOR.MINOR.PATCH
MAJOR.MINOR.PATCH-suffix
```

Examples:

```text
0.4.0
0.4.0-dev
0.5.0-alpha.1
1.0.0
```

Git commit hashes are useful for traceability, but they are not the primary
version.

## Meaning

Before `1.0.0`, this repo should be treated as pre-stable:

- `MAJOR`: reserved for the eventual stable compatibility line
- `MINOR`: the main release boundary during `0.x`; may include breaking deployment or migration-contract changes
- `PATCH`: bug fixes and idempotence fixes that do not intentionally change the desired setup contract

After `1.0.0`, use stricter semver-style meaning:

- `MAJOR`: breaking compatibility or migration-contract changes
- `MINOR`: backward-compatible features or additive setup/state changes
- `PATCH`: bug fixes and idempotence fixes that do not change the migration contract

## Installed State

Each machine should record local install state outside the tracked source tree.

Current install state path:

`/var/lib/sensos-server/install-state.env`

Current fields written by setup:

```sh
INSTALLED_VERSION=0.4.0
REPO_ROOT=/home/sensos/sensos-server
SERVICE_NAME=sensos-server
SERVICE_USER=sensos
```

This file is machine-local state. It is not the repo's source of truth.

## What To Do

Before a release:

- decide whether the change is `MAJOR`, `MINOR`, or `PATCH`
- bump [`VERSION`](../VERSION) intentionally
- if the release changes persisted state or setup behavior, add or update migration logic

During `0.x`, prefer `MINOR` bumps for meaningful deployment-model, service, or
setup-contract changes, even when they are not backward-compatible.

## Working Mode During Active Stabilization

While the repo is still in a high-churn bug-swatting phase, use a rolling
`-dev` version for the current migration boundary.

Example:

```text
0.4.0-dev
```

In this mode:

- do not bump `PATCH` for every bug fix or idempotence fix
- keep the same `MAJOR.MINOR.PATCH-dev` while you are stabilizing one intended deployment contract
- bump `MINOR` when the migration boundary changes in a meaningful way

Examples of meaningful boundary changes:

- service renames
- command renames
- state-layout changes
- install or upgrade contract changes

## Upgrade Flow

The repo includes a top-level [`upgrade`](../upgrade) script for the normal
update flow. It:

- requires a clean git worktree before pulling
- uses `git pull --ff-only`
- runs version-aware migrations from [`migrations/run`](../migrations/run)
- reruns setup
- optionally restarts the service

The repo also includes [`bin/install-service`](../bin/install-service) for the
optional systemd integration step. It prompts with a `[y/N]` warning, then runs
the repo's setup scripts and installs host integration around the live repo
checkout. The legacy top-level [`install`](../install) path remains only as a
compatibility wrapper.

## Reminder

- do not use git SHA as the migration key
- do use git SHA as trace metadata
- keep migrations idempotent
- record installed version only after successful completion
