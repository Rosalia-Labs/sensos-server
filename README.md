# sensos-server

Server component for SensOS.

This repo now uses a standalone server layout at the repository root:

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
