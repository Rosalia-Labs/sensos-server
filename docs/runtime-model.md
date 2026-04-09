# Runtime Model

This repo intentionally uses a single runtime-oriented repository root.

The checked-out repo is the live server tree. This repo does not build a
separate overlay like `sensos-client`; instead, it runs the Docker stack
directly from the checkout and treats systemd as optional host integration.

## Why This Model

The client and server are different.

For `sensos-client`, the repo is a source tree that builds and deploys a host
overlay into `/sensos`.

For `sensos-server`, most of the runtime already happens inside Docker. The host
mainly needs:

- Docker and `docker-compose`
- `curl` for helper scripts such as `bin/create-network`
- a configured repo checkout
- a user who can actually run Docker

Optional:

- a systemd unit that points at that checkout

Because of that, keeping the repo itself as the runtime tree is a reasonable
operating model and works well with a normal non-system user such as `sensos`.
That user does not need `sudo`, but does need permission to talk to Docker,
typically by being in the `docker` group.
Privileged host actions should be done from a separate admin account rather
than by granting `sudo` to the service user.

On Debian-family systems, install `docker-cli`, `docker.io`,
`docker-compose`, and `curl`.

## Expected Layout

Key paths at the repo root:

- `bin/` operational scripts
- `docker/` compose stack and container sources
- `setup/` host integration steps
- `migrations/` upgrade hooks
- `test/` local test helpers

Primary entrypoints:

- `./bin/configure-server`
- `./bin/start-server`
- `./bin/stop-server`
- `./bin/install-service`
- `./upgrade`

For the current DB-backed container orchestration model, see
[`docs/container-control-plane.md`](container-control-plane.md).

## Runtime State

The repo contains both source and a small amount of machine-local runtime state.
That state is kept in ignored paths.

Main ignored runtime paths:

- `docker/.env`
- `docker/.env.bak`
- `backups/`
- `local/`
- `test/qemu/artifacts/`

Legacy material remains under `SensOS/` for inspection during the migration,
but it is not part of the active standalone server layout.

## Install Behavior

`./bin/install-service` does not copy the repo into another deploy root.

Instead, it:

- validates the checkout
- installs a systemd unit
- enables that service

This is optional. It exists to get automatic startup and recovery across host
reboots. The service still runs the same repo-owned start script that you can
invoke manually.

Because installing a unit touches privileged host state, a privileged user must
run `./bin/install-service` or install the unit manually.

## Default Operation

The default operating model is manual unprivileged use from the repo checkout:

1. `./bin/configure-server`
2. `./bin/start-server`
3. `./bin/create-network <network-name>`

That is the baseline path to document and support.

At runtime, the controller acts as the API and schema bootstrap service while
the WireGuard-capable containers reconcile their own local state from the
database. The database is the control plane for local container coordination.

If a machine owner wants automatic startup after reboot, they can install the
optional systemd unit separately.

## Upgrade Behavior

`./upgrade` follows the same basic pattern as `sensos-client`, but applied to
the repo-root runtime model:

- verify the checkout and current server config
- optionally `git pull --ff-only`, exiting early when it does not move `HEAD`
- run version-aware migrations
- record installed version state without requiring root
- rebuild and restart the running Docker stack when the server is already up
- optionally refresh the systemd unit from an admin account
- optionally restart the service from an admin account

This keeps the operational flow familiar without forcing a separate deployed
overlay model that the Docker-first server does not need.
