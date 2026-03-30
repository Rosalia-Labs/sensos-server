# User Commands

This page documents the main user-facing commands in this repo: the normal
bring-up sequence, what each command does, and the commands you are likely to
run during setup, upgrade, or debugging.

## Typical Setup Sequence

Typical order on a newly prepared server host:

1. `./bin/configure-server.sh`
2. `./bin/start-server.sh`
3. optional: `./install`
4. optional: `sudo systemctl start sensos-server`

Notes:

- the default path is direct unprivileged use from the repo
- the systemd service is optional and exists for reboot persistence and uptime
- this repo is designed to run directly from the checkout owned by the service user
- the current user must be able to run Docker, usually by being in the `docker` group

## Top-Level Repo Commands

### `./install`

Installs the optional host integration needed to run the server under systemd
from the current repo checkout.

Typical use:

```sh
./install
```

Behavior:

- must be run as the repo owner, not `root`
- requires a privileged user path via `sudo`
- confirms the target repo path
- runs the setup pipeline with `sudo` for the privileged steps
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
- does not require `sudo` unless you ask it to refresh the optional service install
- `--offline` skips `git pull` and upgrades from the repo contents already on disk
- `--refresh-service` reinstalls the optional `sensos-server` unit after pull
- `--restart-service` restarts `sensos-server` and requires `--refresh-service`

## Core Server Commands

### `bin/configure-server.sh`

Writes the Docker environment file used by the server stack.

Important flags from the source:

- `--db-port`
- `--api-port`
- `--wg-network`
- `--wg-server-ip`
- `--wg-port`
- `--postgres-password`
- `--api-password`
- `--expose-containers`

Typical use:

```sh
./bin/configure-server.sh
./bin/configure-server.sh --api-port 8765 --wg-network testing
```

Behavior:

- writes `docker/.env`
- backs up an existing file to `docker/.env.bak`
- does not start containers by itself
- does not require `sudo`

### `bin/start-server.sh`

Starts the Docker Compose stack from the repo-owned `docker/` directory.

Typical use:

```sh
./bin/start-server.sh
./bin/start-server.sh --rebuild-containers
./bin/start-server.sh --restart
./bin/start-server.sh --no-detach
```

Behavior:

- requires `docker/.env`
- requires that the current user can talk to Docker
- loads repo version and git metadata into the container build/runtime environment
- refuses to start if SensOS containers are already running, unless `--restart` is supplied
- can rebuild containers before start

### `bin/stop-server.sh`

Stops the Docker Compose stack.

Typical use:

```sh
./bin/stop-server.sh
./bin/stop-server.sh --backup
./bin/stop-server.sh --remove-volumes
```

Behavior:

- can back up database and WireGuard state first
- `--remove-volumes` tears down named volumes
- `--no-backup` suppresses the backup step even when removing volumes

### `bin/backup-database.sh`

Creates a gzipped PostgreSQL backup under `backups/`.

Typical use:

```sh
./bin/backup-database.sh
```

### `bin/backup-wireguard.sh`

Backs up WireGuard config files from running SensOS containers into `backups/`.

Typical use:

```sh
./bin/backup-wireguard.sh
```

### `bin/backup-server.sh`

Runs the standard server backup set and can optionally export those backups
with `rclone` and/or run a user-supplied post-backup hook.

Typical use:

```sh
./bin/backup-server.sh
./bin/backup-server.sh --export --remote box:sensos-server-backups
./bin/backup-server.sh --export --remote box:sensos-server-backups --move
./bin/backup-server.sh --post-hook ./local/hooks/post-backup.sh
```

Behavior:

- runs `bin/backup-database.sh`
- runs `bin/backup-wireguard.sh`
- keeps local backups by default
- can export to a configured `rclone` remote after backup creation
- can run a user-owned post-backup hook
- looks for a default local hook at `local/hooks/post-backup.sh`
- see [`docs/backup-automation.md`](backup-automation.md) for cron and hook examples

Hook contract:

- argv[1] is the backup directory
- argv[2+] are the newly created backup files from this run
- local hook scripts belong in ignored repo-local state such as `local/hooks/`

### `bin/export-backups.sh`

Exports existing backup artifacts from `backups/` using `rclone`.

Typical use:

```sh
./bin/export-backups.sh --remote box:sensos-server-backups
./bin/export-backups.sh --remote box:sensos-server-backups --move
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

### `bin/update-client`

Sync the standalone `sensos-client` overlay tree to a remote client through the
controller container.

Typical use:

```sh
./bin/update-client testing_1_15
./bin/update-client testing_1_15 --dry-run
./bin/update-client testing_1_15 --reboot
```

Behavior:

- reads the client payload from `sensos-client/overlay` by default
- allows override with `SENSOS_CLIENT_REPO_ROOT`
- excludes client data, keys, logs, and init-state paths from destructive sync
