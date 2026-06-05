# Backup Automation

This repo supports three backup automation patterns:

1. local backups only
2. local backups plus built-in `rclone` export
3. local backups plus a user-owned post-backup hook

The normal backup entrypoint is:

- [`bin/backup-server`](../bin/backup-server)

`bin/backup-server` creates one top-level artifact per run:

- `server_backup_*.tgz`

If that file would exceed `SENSOS_BACKUP_MAX_ARTIFACT_BYTES`, it is split into
Box-safe parts instead:

- `server_backup_*.parts.txt`
- `server_backup_*.tgz.part-0000`
- `server_backup_*.tgz.part-0001`
- ...

The default split threshold is `14000000000` bytes, which stays below a 15 GB
single-file upload ceiling. To reassemble a split bundle, concatenate the parts
in filename order:

```sh
cat server_backup_20260605_164120.tgz.part-* > server_backup_20260605_164120.tgz
```

That bundle contains:

- `manifest.txt` with creation metadata and component names
- `SHA256SUMS` for the bundled component files
- `db_backup_*.gz` PostgreSQL dump
- `wg_wireguard_*.tgz` for the server WireGuard reconciler state
- `wg_api-proxy_*.tgz` for the API proxy WireGuard reconciler state
- `wg_ops_*.tgz` for the ops WireGuard and operator SSH state

The WireGuard archives intentionally follow the current container ownership
model. They include the owning container's private state directory under
`/var/lib/sensos-*` and any rendered `/etc/wireguard/*.conf` files. The
database backup stores public keys and peer/network rows; these WireGuard
archives store the private key material needed to preserve the existing
container identities. The component files are removed from `backups/` after
the bundle is created, so normal scheduled runs leave one logical backup set
per run.

## Recommended Model

Keep the tracked repo generic and put site-specific automation in ignored local
state under:

`local/`

That path is gitignored, so a user without push access can still install local
backup hooks and other machine-specific scripts.

Default local post-hook path:

`local/hooks/post-backup.sh`

If that file exists and is executable, `bin/backup-server` will run it
automatically after creating backups.

## Cron Example

Example crontab entry for the repo-owning user:

```cron
MAILTO=""
PATH=/usr/local/bin:/usr/bin:/bin

# Run server backups every day at 02:15.
15 2 * * * cd /home/sensos/sensos-server && ./bin/backup-server >> /home/sensos/sensos-server/local/log/backup-cron.log 2>&1
```

If you want the built-in `rclone` export to Box:

```cron
MAILTO=""
PATH=/usr/local/bin:/usr/bin:/bin

# Run backups and copy them to Box every day at 02:15.
15 2 * * * cd /home/sensos/sensos-server && ./bin/backup-server --export --remote box:sensos-server-backups >> /home/sensos/sensos-server/local/log/backup-cron.log 2>&1
```

You can install that with:

```sh
crontab -e
```

## Post-Hook Example

The post-hook contract is:

- argv[1] is the backup directory
- argv[2+] are the newly created top-level backup bundles from this run

Example local hook using the built-in export helper:

```bash
#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_DIR="${1:?missing backup dir}"
shift || true

"${REPO_ROOT}/bin/export-backups" --remote box:sensos-server-backups --copy
```

Place a real local version at:

`local/hooks/post-backup.sh`

## Other Mechanisms

If you do not want cron, the same backup command works fine from:

- a systemd user timer
- a manually installed root-owned system timer that runs as `sensos`
- a CI runner or remote management tool

The command to schedule is still the same:

```sh
cd /path/to/sensos-server
./bin/backup-server
```

or:

```sh
cd /path/to/sensos-server
./bin/backup-server --export --remote box:sensos-server-backups
```

or:

```sh
cd /path/to/sensos-server
./bin/backup-server --post-hook ./local/hooks/post-backup.sh
```
