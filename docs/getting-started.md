# Getting Started (Tutorial)

This guide is the shortest path to bring up a SensOS server host.
For full command syntax, flags, and operational details, use the
[`Command Reference`](command-reference.md).

## Before You Start

- Use a Debian-family host with Docker installed and available to your server user.
- Clone this repo as the long-lived server runtime checkout.
- Confirm the account that will operate the server can run Docker commands.

Reference: [Server user setup](server-user-setup.md)

## 1. Configure Server Environment

From the repo root:

```sh
./bin/configure-server
```

This writes `docker/.env` (ports, API credentials, dashboard settings).

Reference: [`bin/configure-server`](command-reference.md#binconfigure-server)

## 2. Start The Control Plane

```sh
./bin/start-server
```

Reference:

- [`bin/start-server`](command-reference.md#binstart-server)
- [Container control plane](container-control-plane.md)

## 3. Create A Client Network

```sh
./bin/create-network <network-name>
```

This publishes a WireGuard endpoint for client enrollment.

Reference:

- [`bin/create-network`](command-reference.md#bincreate-network)
- [Networking](networking.md)

## 4. Enroll Clients

Use the server endpoint details from step 3 on client devices with
`config-network` in `sensos-client`.

Reference:

- [sensos-client getting started](https://github.com/Rosalia-Labs/sensos-client/blob/main/docs/getting-started.md)
- [`bin/client-overview`](command-reference.md#binclient-overview)
- [`bin/network-overview`](command-reference.md#binnetwork-overview)

## 5. Optional Host Integration

For reboot-persistent systemd management on the host:

```sh
sudo /path/to/sensos-server/bin/install-service
```

Reference: [`bin/install-service`](command-reference.md#bininstall-service)

## Ongoing Operations

- Upgrade in place: `./upgrade`
- Logs: `./bin/server-logs --follow`
- Reset local stack (destructive to runtime volumes): `./bin/reset-server`

Reference:

- [`./upgrade`](command-reference.md#upgrade)
- [`bin/server-logs`](command-reference.md#binserver-logs)
- [`bin/reset-server`](command-reference.md#binreset-server)

## Related Docs

- [Runtime model](runtime-model.md)
- [Backup automation](backup-automation.md)
- [QEMU testing](qemu-testing.md)
