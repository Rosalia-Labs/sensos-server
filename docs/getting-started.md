# Getting Started (Tutorial)

This guide is the shortest path to bring up a SensOS server host.
For full command syntax, flags, and operational details, use the
[`Command Reference`](command-reference.md).

## Before You Start

- Use a Debian-family host.
- Plan to run SensOS from a dedicated unprivileged service account (for example `sensos`).
- Keep a separate admin account for host-level `sudo` tasks.

Reference: [Server user setup](server-user-setup.md)

## 1. Prepare Host Packages (Admin Account)

Install required host packages and Docker prerequisites using the canonical
notes in [`command-reference.md`](command-reference.md#typical-setup-sequence).

## 2. Create Service User And Docker Access (Admin Account)

Complete these canonical steps in [Server user setup](server-user-setup.md):

- [Create the user](server-user-setup.md#create-the-user)
- [Install SSH keys](server-user-setup.md#install-ssh-keys)
- [Add Docker access](server-user-setup.md#add-docker-access)

Then log in as that service user before continuing.

## 3. Clone Repo As Service User

Use the canonical clone instructions here:
[Clone the repo as that user](server-user-setup.md#clone-the-repo-as-that-user).
Run the remaining steps from that checkout as the service user.

## 4. Configure Server Environment

From the repo root:

```sh
./bin/configure-server
```

This writes `docker/.env` (ports, API credentials, dashboard settings).

Reference: [`bin/configure-server`](command-reference.md#binconfigure-server)

## 5. Start The Control Plane

```sh
./bin/start-server
```

Reference:

- [`bin/start-server`](command-reference.md#binstart-server)
- [Container control plane](container-control-plane.md)

## 6. Create A Client Network

```sh
./bin/create-network <network-name>
```

This publishes a WireGuard endpoint for client enrollment.

Reference:

- [`bin/create-network`](command-reference.md#bincreate-network)
- [Networking](networking.md)

## 7. Enroll Clients

Use the server endpoint details from step 6 on client devices with
`config-network` in `sensos-client`.

Reference:

- [sensos-client getting started](https://github.com/Rosalia-Labs/sensos-client/blob/main/docs/getting-started.md)
- [`bin/client-overview`](command-reference.md#binclient-overview)
- [`bin/network-overview`](command-reference.md#binnetwork-overview)

## 8. Optional Host Integration (Admin Account)

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
