# User Commands

This page documents the main user-facing commands in this repo: the normal
bring-up sequence, what each command does, and the commands you are likely to
run during setup, upgrade, or debugging.

For the client/server network contract, see [`docs/networking.md`](networking.md).
For the current server container orchestration model, see
[`docs/container-control-plane.md`](container-control-plane.md).

## Typical Setup Sequence

Typical order on a newly prepared server host:

1. `./bin/configure-server`
2. `./bin/start-server`
3. `./bin/create-network <network-name>`
4. optional: `./bin/install-service`
5. optional: `sudo systemctl start sensos-server`

Notes:

- the default path is direct unprivileged use from the repo
- the systemd service is optional and exists for reboot persistence and uptime
- this repo is designed to run directly from the checkout owned by the service user
- the current user must be able to run Docker, usually by being in the `docker` group
- see [`docs/server-user-setup.md`](server-user-setup.md) for a dedicated host-user bootstrap example
- on Debian-family systems, install `docker.io`, `docker-compose`, and `curl`

## Optional Host Integration

### `bin/install-service`

Installs the optional host integration needed to run the server under systemd
from the current repo checkout.

Typical use:

```sh
./bin/install-service
sudo /home/sensos/sensos-server/bin/install-service
```

Behavior:

- can be run by the repo owner if that user has `sudo`
- can also be run directly by an admin account with `sudo /path/to/repo/bin/install-service`
- confirms the target repo path
- infers the service user from the repo checkout owner unless `SENSOS_SERVICE_USER` is set
- runs the setup pipeline with a privileged path
- installs and enables the `sensos-server` systemd unit
- leaves the runtime code in the repo instead of deploying a separate overlay
- is not required for normal manual operation

### `./upgrade`

Pulls the latest repo changes and runs migrations for the repo-owned server
checkout.

Typical use:

```sh
./upgrade
./upgrade --offline
./upgrade --refresh-service
./upgrade --refresh-service --restart-service
```

Behavior:

- in normal mode, it must be run from a clean git worktree
- in normal mode, the current branch must have an upstream
- runs migrations between installed and repo versions
- records the upgraded version in a writable install-state file
- if the server stack is already running, it rebuilds and restarts the containers so the new repo contents take effect
- does not require `sudo` for the normal repo-owned Docker runtime path
- `--offline` skips `git pull` and upgrades from the repo contents already on disk
- `--refresh-service` reinstalls the optional `sensos-server` unit after pull and should be run from an admin account with `sudo`
- `--restart-service` restarts `sensos-server`, requires `--refresh-service`, and should be run from an admin account with `sudo`

## Core Server Commands

### `bin/configure-server`

Writes the Docker environment file used by the server stack.

Important flags from the source:

- `--db-port`
- `--api-port`
- `--postgres-password`
- `--api-password`
- `--expose-containers`

Typical use:

```sh
./bin/configure-server
./bin/configure-server --api-port 8765 --api-password '<password>'
```

Behavior:

- writes `docker/.env`
- backs up an existing file to `docker/.env.bak`
- does not start containers by itself
- does not require `sudo`

### `bin/create-network`

Creates or reconciles a named client network through the running server API.

Typical use:

```sh
./bin/create-network testing --wg-port 51820
./bin/create-network biosense --wg-port 51821
./bin/create-network testing --wg-public-ip server.example.org --wg-port 51820
./bin/create-network testing --config-server 127.0.0.1 --port 8765
```

Behavior:

- requires the server API to already be running
- defaults `wg_public_ip` from `docker/.env` or by resolving the host's public IPv4 address at runtime
- `--wg-public-ip` overrides the detected/default endpoint value
- still requires explicit `--wg-port` because that is a network property
- defaults the API password from `docker/.env`
- creates no network automatically at server startup
- prints the resulting CIDR, WireGuard endpoint, and a sample client enrollment command

### `bin/start-server`

Starts the Docker Compose stack from the repo-owned `docker/` directory.

Typical use:

```sh
./bin/start-server
./bin/start-server --rebuild-containers
./bin/start-server --restart
./bin/start-server --no-detach
```

Behavior:

- requires `docker/.env`
- requires that the current user can talk to Docker
- loads repo version and git metadata into the container build/runtime environment
- refuses to start if SensOS containers are already running, unless `--restart` is supplied
- can rebuild containers before start
- starts the DB-backed control-plane stack, where the database coordinates
  container WireGuard reconciliation

### `bin/stop-server`

Stops the Docker Compose stack.

Typical use:

```sh
./bin/stop-server
./bin/stop-server --backup
./bin/stop-server --remove-volumes
```

Behavior:

- can back up database and WireGuard state first
- `--remove-volumes` tears down named volumes
- `--no-backup` suppresses the backup step even when removing volumes

### `bin/backup-database`

Creates a gzipped PostgreSQL backup under `backups/`.

Typical use:

```sh
./bin/backup-database
```

### `bin/backup-wireguard`

Backs up WireGuard config files from running SensOS containers into `backups/`.

Typical use:

```sh
./bin/backup-wireguard
```

### `bin/backup-server`

Runs the standard server backup set and can optionally export those backups
with `rclone` and/or run a user-supplied post-backup hook.

Typical use:

```sh
./bin/backup-server
./bin/backup-server --export --remote box:sensos-server-backups
./bin/backup-server --export --remote box:sensos-server-backups --move
./bin/backup-server --post-hook ./local/hooks/post-backup.sh
```

Behavior:

- runs `bin/backup-database`
- runs `bin/backup-wireguard`
- keeps local backups by default
- can export to a configured `rclone` remote after backup creation
- can run a user-owned post-backup hook
- looks for a default local hook at `local/hooks/post-backup.sh`
- see [`docs/backup-automation.md`](backup-automation.md) for cron and hook examples

Hook contract:

- argv[1] is the backup directory
- argv[2+] are the newly created backup files from this run
- local hook scripts belong in ignored repo-local state such as `local/hooks/`

### `bin/export-backups`

Exports existing backup artifacts from `backups/` using `rclone`.

Typical use:

```sh
./bin/export-backups --remote box:sensos-server-backups
./bin/export-backups --remote box:sensos-server-backups --move
```

Behavior:

- requires `rclone`
- exports `db_backup_*.gz` and `wg_*.tgz`
- `--copy` keeps local files
- `--move` removes local files only after successful transfer
- useful as the built-in implementation behind a custom post-backup hook

### `bin/ssh-client`

SSH into a remote client through the controller container using either a direct
IP or the network token form used by the project.

Typical use:

```sh
./bin/ssh-client 10.23.1.15
./bin/ssh-client testing_1_15
./bin/ssh-client testing_1_15 -- hostname
```
