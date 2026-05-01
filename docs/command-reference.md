# User Commands

For a step-by-step bring-up path, start with the [Getting Started Tutorial](getting-started.md).

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
- on Debian-family systems, install `docker-cli`, `docker.io`,
  `docker-compose`, and `curl`

## Optional Host Integration

### `bin/install-service`

Installs the optional host integration needed to run the server under systemd
from the current repo checkout.

Typical use:

```sh
sudo /home/sensos/sensos-server/bin/install-service
```

Behavior:

- should be run from a separate admin account with `sudo /path/to/repo/bin/install-service`
- the `sensos` service user is not expected to have `sudo`
- confirms the target repo path
- infers the service user from the repo checkout owner unless `SENSOS_SERVICE_USER` is set
- runs the setup pipeline with a privileged path
- installs and enables the `sensos-server` systemd unit
- prints a clear hint to rerun from a privileged account if the privileged step fails
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
- in normal mode, it exits early when `git pull --ff-only` does not change `HEAD`
- runs migrations between installed and repo versions
- records the upgraded version in a writable install-state file
- if the server stack is already running, it rebuilds and restarts the containers so the new repo contents take effect
- does not require `sudo` for the normal repo-owned Docker runtime path
- `--offline` skips `git pull` and upgrades from the repo contents already on disk, including force-running an upgrade when the checkout has not changed
- `--refresh-service` reinstalls the optional `sensos-server` unit after pull and should be run from an admin account with `sudo`
- `--restart-service` restarts `sensos-server`, requires `--refresh-service`, and should be run from an admin account with `sudo`

### `./uninstall`

Stops the local server stack and removes optional host integration for this repo
checkout.

Typical use:

```sh
./uninstall
./uninstall --purge-data
./uninstall --purge-data --purge-images
./uninstall --purge-data --no-backup
./uninstall --keep-service
```

Behavior:

- stops the Docker stack through `bin/stop-server`
- removes the optional `sensos-server` systemd unit by default
- removes install-state markers used by `./upgrade`
- `--purge-data` also removes Docker volumes and `/var/lib/sensos-server` for a fresh reinstall
- `--purge-images` also removes Docker images referenced by this repo's Compose stack
- `--purge-data` keeps the normal backup behavior unless `--no-backup` is passed
- preserves the repo checkout, `docker/.env`, and repo backups
- prompts for confirmation unless `--yes` is passed

## Core Server Commands

### `bin/reset-server`

Resets the local server stack in place by deleting Docker volumes, then
rebuilding and starting the stack again.

Typical use:

```sh
./bin/reset-server
./bin/reset-server --yes
./bin/reset-server --debug
```

Behavior:

- stops the Docker stack through `bin/stop-server --remove-volumes --no-backup`
- wipes the database and other Docker volume-backed runtime state
- rebuilds containers and starts them detached by default
- `--debug` keeps the rebuilt stack attached in the foreground for live debugging
- preserves the repo checkout, `docker/.env`, and host integration
- prompts for confirmation unless `--yes` is passed

### `bin/configure-server`

Writes the Docker environment file used by the server stack.

Important flags from the source:

- `--api-port`
- `--public-ui-port`
- `--postgres-password`
- `--admin-api-password`
- `--client-api-password`
- `--public-db-password`
- `--ui-theme` (`default` or `vscode-dark`; omitted keeps built-in default look)

Typical use:

```sh
./bin/configure-server
./bin/configure-server --api-port 8765 --admin-api-password '<admin-password>' --client-api-password '<client-password>'
./bin/configure-server --public-ui-port 8780 --public-db-password '<public-db-password>'
```

Behavior:

- writes `docker/.env`
- backs up an existing file to `docker/.env.bak`
- does not start containers by itself
- does not require `sudo`
- configures the published public dashboard port and the read-only public dashboard DB credential

### `bin/create-network`

Creates or reconciles a named client network through the running server API.

Typical use:

```sh
./bin/create-network testing
./bin/create-network biosense
./bin/create-network testing --wg-public-host server.example.org --wg-port 51820
```

Behavior:

- requires the server API to already be running
- runs against the locally configured server API on `127.0.0.1` and the configured API port
- defaults `wg_public_ip` from `docker/.env` or by resolving the host's public IPv4 address at runtime
- defaults `wg_port` by allocating the next free public WireGuard port in `51281..51289`
- with the default port range, automatic allocation supports at most 9 networks before manual port exposure/config changes are required
- `--wg-public-host` and `--wg-port` are the client-facing WireGuard endpoint stored on the network for later use by clients
- `--wg-public-host` may be either a hostname or a literal IP address
- defaults the API password from `docker/.env`
- creates no network automatically at server startup
- if a named network already exists with a different published endpoint, the
  command fails with a clear conflict and does not mutate the existing network
- stops with a clear error if no default WireGuard port remains available
- prints the resulting CIDR, WireGuard endpoint, and a sample client enrollment command

### `bin/update-network-endpoint`

Updates the published client-facing WireGuard endpoint for an existing network.

Typical use:

```sh
./bin/update-network-endpoint testing --wg-public-host 10.0.2.2 --wg-port 51281
```

Behavior:

- requires the server API to already be running
- runs against the locally configured server API on `127.0.0.1` and the configured API port
- requires an existing network name
- updates only the published WireGuard endpoint fields used by clients
- avoids direct database editing for environment-specific corrections such as QEMU host forwarding
- prints the resulting CIDR, WireGuard endpoint, and a sample client enrollment command

### `bin/delete-network`

Deletes a network through the running server API. This is destructive and
cascades to the network's registered peers and related state.

Typical use:

```sh
./bin/delete-network testing
```

Behavior:

- requires the server API to already be running
- runs against the locally configured server API on `127.0.0.1` and the configured API port
- prompts for confirmation before making the request
- deletes the network row, which cascades to peer records, keys, client status,
  location rows, hardware profiles, and runtime status
- defaults the API password from `docker/.env`

### `bin/delete-client`

Deletes a registered client peer through the running server API.

Typical use:

```sh
./bin/delete-client 10.23.1.15
```

Behavior:

- requires the server API to already be running
- runs against the locally configured server API on `127.0.0.1` and the configured API port
- prompts for confirmation before making the request
- deletes the peer row, which cascades to peer keys, client status, location,
  and hardware-profile rows
- defaults the API password from `docker/.env`

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

### `bin/server-logs`

Shows Docker Compose logs for this repo's server stack from the repo-owned
`docker/` directory.

Typical use:

```sh
./bin/server-logs
./bin/server-logs --follow
./bin/server-logs --tail 200
./bin/server-logs --follow api
```

Behavior:

- automatically runs from the repo's `docker/` directory
- prefers `docker compose`, falling back to `docker-compose` when needed
- forwards all arguments directly to `docker compose logs`

### `bin/server-overview`

Prints a compact status summary for the repo, local server config, Docker
runtime, and database-backed network state.

Typical use:

```sh
./bin/server-overview
```

Behavior:

- shows repo version and git state
- shows whether `docker/.env` exists and the configured API port
- shows Docker/container status for the main SensOS services
- if `sensos-database` is running, shows a short database summary including
  network, peer, key, and runtime-status counts

### `bin/network-overview`

Prints a denser single-screen summary focused on configured networks and
registered client state. When given a network name, it switches to a detail
view for that one network.

Typical use:

```sh
./bin/network-overview
./bin/network-overview --networks 20 --clients 12
./bin/network-overview testing
```

Behavior:

- requires Docker access and a running `sensos-database` container
- shows one compact row per network with CIDR, WireGuard endpoint, runtime
  readiness, peer counts, and freshest client check-in age
- shows a short trailing table of registered clients, including peers that have
  never checked in; the client label prefers the peer note and falls back to
  the WireGuard IP
- if a network name is provided, prints runtime rows plus the clients attached
  to that network instead of the global summary
- defaults to `12` network rows and `8` client rows to keep output within a
  terminal screen, with limits adjustable via flags

### `bin/client-overview`

Prints a client-focused summary. Without arguments it lists recent/registered
clients; with a client selector it prints one client in detail.

Typical use:

```sh
./bin/client-overview
./bin/client-overview 12
./bin/client-overview 10.23.1.15
./bin/client-overview client-hostname
```

Behavior:

- requires Docker access and a running `sensos-database` container
- list mode shows a compact table including stable peer ids, network, a client
  label that prefers the peer note and falls back to the WireGuard IP, latest
  hostname, version, and status
- detail mode accepts peer id, peer UUID, WireGuard IP, or exact latest
  hostname
- detail mode prints registration/check-in metadata, latest status, key
  information, location, and hardware-profile summary when present
- when enrolling devices, use a unique note for each client so the overview
  tables show meaningful names instead of only IP addresses
- if a client later gets a new WireGuard IP, keep the same note so operators
  still recognize it as the same device in overview output

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

SSH into a remote client through the ops container using either a direct
IP or the network token form used by the project.

Typical use:

```sh
./bin/ssh-client 10.23.1.15
./bin/ssh-client testing_1_15
./bin/ssh-client testing_1_15 -- hostname
```

Behavior:

- runs SSH from the dedicated `sensos-ops` container rather than from `sensos-controller`
- uses lower-overhead SSH defaults suitable for metered links where supported by the bundled SSH client

### `bin/listen-client-audio`

Streams temporary live audio from a client and plays it on the operator
machine. This uses `bin/ssh-client` plus the client's `debug-audio-monitor`
helper.

Typical use:

```sh
./bin/listen-client-audio testing-0-1
./bin/listen-client-audio testing-0-1 --duration 30
```

Local playback dependencies:

- `listen-client-audio` prefers `play` from `sox`
- on macOS, install it with Homebrew: `brew install sox`
- on macOS, install it with MacPorts: `sudo port install sox`
- on Debian-family systems, `play` is usually provided by the `sox` package
- if `play` is unavailable, the wrapper falls back to `aplay` when present

Behavior:

- requires the client helper `debug-audio-monitor` to be installed on the device
- temporarily stops `sensos-record-audio.service` on the client for the duration of the listen session
- prefers local playback through `play` and falls back to `aplay` when available
- relies on the client helper to restart recording automatically when the session ends

## Admin UI

The built-in admin UI now includes a `BirdNET` view at `/admin/birdnet`.

Behavior:

- shows recent accepted BirdNET upload batches
- summarizes total uploaded batches and processed-file records stored on the server
- helps confirm that client-side BirdNET uploads are arriving without needing direct database inspection

## Public Dashboard

The public dashboard runs as a separate `sensos-public-ui` service in the same
Docker stack and is published on the configured `PUBLIC_UI_PORT`, which
defaults to `8780`.

Behavior:

- serves a standalone public map-first dashboard
- reads only curated public SQL views through a dedicated read-only database role
- stays separate from the admin/controller process even though it shares the same repo and compose stack
