# Runtime Model

This repo intentionally uses a single runtime-oriented repository root.

The checked-out repo is the live server tree. The install flow does not build a
separate overlay like `sensos-client`; instead, it performs thin host
integration around the repo-owned Docker stack.

## Why This Model

The client and server are different.

For `sensos-client`, the repo is a source tree that builds and deploys a host
overlay into `/sensos`.

For `sensos-server`, most of the runtime already happens inside Docker. The host
mainly needs:

- Docker and Docker Compose
- a configured repo checkout
- a systemd unit that points at that checkout

Because of that, keeping the repo itself as the runtime tree is a reasonable
operating model and works well with a normal non-system user such as `sensos`.

## Expected Layout

Key paths at the repo root:

- `bin/` operational scripts
- `docker/` compose stack and container sources
- `setup/` host integration steps
- `migrations/` upgrade hooks
- `test/` local test helpers

Primary entrypoints:

- `./bin/configure-server.sh`
- `./bin/start-server.sh`
- `./bin/stop-server.sh`
- `./install`
- `./upgrade`

## Runtime State

The repo contains both source and a small amount of machine-local runtime state.
That state is kept in ignored paths.

Main ignored runtime paths:

- `docker/.env`
- `docker/.env.bak`
- `backups/`
- `test/qemu/artifacts/`

Legacy material remains under `SensOS/` for inspection during the migration,
but it is not part of the active standalone server layout.

## Install Behavior

`./install` does not copy the repo into another deploy root.

Instead, it:

- validates the checkout
- installs a systemd unit
- enables that service
- records minimal install state

The service then runs the server directly from this checkout as the configured
service user.

## Upgrade Behavior

`./upgrade` follows the same basic pattern as `sensos-client`, but applied to
the repo-root runtime model:

- verify the checkout and current server config
- optionally `git pull --ff-only`
- run version-aware migrations
- rerun setup
- optionally restart the service

This keeps the operational flow familiar without forcing a separate deployed
overlay model that the Docker-first server does not need.
