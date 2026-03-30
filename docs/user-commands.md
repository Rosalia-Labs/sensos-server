# User Commands

This page documents the main user-facing commands in this repo: the normal
bring-up sequence, what each command does, and the commands you are likely to
run during setup, upgrade, or debugging.

## Typical Setup Sequence

Typical order on a newly prepared server host:

1. `./bin/configure-server.sh`
2. `./install`
3. `sudo systemctl start sensos-server`
4. `./bin/start-server.sh --restart` for interactive restart-driven work when needed

Notes:

- the normal long-lived path is the systemd service
- the direct `bin/` scripts are still useful for debugging and local iteration
- this repo is designed to run directly from the checkout owned by the service user

## Top-Level Repo Commands

### `./install`

Installs the host integration needed to run the server from the current repo
checkout.

Typical use:

```sh
./install
```

Behavior:

- must be run as the repo owner, not `root`
- confirms the target repo path
- runs the setup pipeline with `sudo` for the privileged steps
- installs and enables the `sensos-server` systemd unit
- leaves the runtime code in the repo instead of deploying a separate overlay

### `./upgrade`

Pulls the latest repo changes, runs migrations, and reapplies setup to the
installed server.

Typical use:

```sh
./upgrade
./upgrade --offline
./upgrade --restart-service
```

Behavior:

- in normal mode, it must be run from a clean git worktree
- in normal mode, the current branch must have an upstream
- runs migrations between installed and repo versions
- reruns setup after pull
- `--offline` skips `git pull` and upgrades from the repo contents already on disk
- `--restart-service` restarts `sensos-server` after setup completes

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
- `--registry-port`
- `--registry-user`
- `--registry-password`
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
