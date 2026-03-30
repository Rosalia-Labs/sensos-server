# sensos-server

Server component for SensOS.

This repo uses a single runtime-oriented repository root. The checked-out repo
is the live server tree, and host integration is intentionally thin: configure
`docker/.env`, install the systemd unit, and run the stack directly from this
checkout as the `sensos` user.

Runtime state that should not be committed is kept in ignored paths such as:

- `docker/.env`
- `docker/.env.bak`
- `backups/`
- `test/qemu/artifacts/`

Standalone server layout:

- `bin/` operational scripts
- `docker/` compose stack and container sources
- `setup/` install-time host reconciliation
- `migrations/` upgrade hooks
- `test/` local test helpers, including QEMU VM launchers

Primary entrypoints:

- `./bin/configure-server.sh`
- `./bin/start-server.sh`
- `./bin/stop-server.sh`
- `./install`
- `./upgrade`

Docs:

- [QEMU testing](./test/qemu/docs/README.md)

Legacy material remains under `SensOS/` for inspection during the migration.
