# Backup Automation

This repo supports three backup automation patterns:

1. local backups only
2. local backups plus built-in `rclone` export
3. local backups plus a user-owned post-backup hook

The normal backup entrypoint is:

- [`bin/backup-server.sh`](../bin/backup-server.sh)

## Recommended Model

Keep the tracked repo generic and put site-specific automation in ignored local
state under:

`local/`

That path is gitignored, so a user without push access can still install local
backup hooks and other machine-specific scripts.

Default local post-hook path:

`local/hooks/post-backup.sh`

If that file exists and is executable, `bin/backup-server.sh` will run it
automatically after creating backups.

## Cron Example

Example crontab entry for the repo-owning user:

```cron
MAILTO=""
PATH=/usr/local/bin:/usr/bin:/bin

# Run server backups every day at 02:15.
15 2 * * * cd /home/sensos/sensos-server && ./bin/backup-server.sh >> /home/sensos/sensos-server/local/log/backup-cron.log 2>&1
```

If you want the built-in `rclone` export to Box:

```cron
MAILTO=""
PATH=/usr/local/bin:/usr/bin:/bin

# Run backups and copy them to Box every day at 02:15.
15 2 * * * cd /home/sensos/sensos-server && ./bin/backup-server.sh --export --remote box:sensos-server-backups >> /home/sensos/sensos-server/local/log/backup-cron.log 2>&1
```

You can install that with:

```sh
crontab -e
```

An example file is included at:

- [`examples/cron/sensos-server-backup.cron.example`](../examples/cron/sensos-server-backup.cron.example)

## Post-Hook Example

The post-hook contract is:

- argv[1] is the backup directory
- argv[2+] are the newly created backup files from this run

Example local hook using the built-in export helper:

```bash
#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_DIR="${1:?missing backup dir}"
shift || true

"${REPO_ROOT}/bin/export-backups.sh" --remote box:sensos-server-backups --copy
```

Place a real local version at:

`local/hooks/post-backup.sh`

An example template is included at:

- [`examples/hooks/post-backup.sh.example`](../examples/hooks/post-backup.sh.example)

## Other Mechanisms

If you do not want cron, the same backup command works fine from:

- a systemd user timer
- a manually installed root-owned system timer that runs as `sensos`
- a CI runner or remote management tool

The command to schedule is still the same:

```sh
cd /path/to/sensos-server
./bin/backup-server.sh
```

or:

```sh
cd /path/to/sensos-server
./bin/backup-server.sh --export --remote box:sensos-server-backups
```

or:

```sh
cd /path/to/sensos-server
./bin/backup-server.sh --post-hook ./local/hooks/post-backup.sh
```
