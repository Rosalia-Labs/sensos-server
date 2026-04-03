# Networking

This page documents the practical network contract between `sensos-server` and
`sensos-client`.

The important distinction is:

- setup-time reachability
- steady-state reachability after WireGuard is configured

## High-Level Model

The server has two relevant externally reachable surfaces:

1. the HTTP API on the configured API port
2. one or more WireGuard UDP ports on the Docker host

The client uses them differently at different stages:

- during enrollment, the client reaches the server API at `http://<config-server>:<port>`
- after enrollment, the client talks to the API over WireGuard using `SERVER_WG_IP` and `SERVER_PORT`

## API Port

The server API is exposed by the Docker host on:

- `API_PORT`, configured in `docker/.env`
- default: `8765`

You set that with:

```sh
./bin/configure-server --api-port 8765
```

During client enrollment, `sensos-client` must be pointed at the same host and
port:

```sh
config-network --config-server <server-host-or-ip> --port 8765 --network <network>
```

`--config-server` is only the address the client can reach during setup. It can
be a LAN IP, hostname, forwarded port target, or QEMU host address.

Operational note:

- if a client does not already have a private way to reach the API port, you may
  choose to expose that port only during setup
- the API is not intended to be a generally public management surface outside of
  WireGuard
- if security is a concern, temporary port forwarding can be a better setup
  path than leaving the API broadly exposed

By contrast, the WireGuard endpoint IP and UDP port are not server-global
settings in this repo. They are chosen per network when you run
`bin/create-network`.

## API Password

The server requires HTTP Basic auth using the configured API password:

- server source of truth: `API_PASSWORD` in `docker/.env`
- client local copy: `/sensos/keys/api_password`

You set the server-side password with:

```sh
./bin/configure-server --api-password '<password>'
```

During enrollment, the client operator must enter the same password when
`config-network` prompts for it.

If they do not match:

- enrollment API calls fail
- later status updates and location sync fail

Current implementation note:

- the server validates the password and does not currently care about the
  Basic-auth username
- the client commonly uses username `sensos`

## WireGuard Host Port Exposure

Each registered network stores:

- a public/reachable endpoint address or hostname
- a WireGuard UDP port

That data is returned to the client during `register-peer`, and the client
writes it into `/etc/wireguard/<network>.conf`.

Peer rows reserve their WireGuard IPs until they are deleted. Marking a peer
inactive does not free its IP; deleting the peer does.

The Docker host must actually expose the chosen UDP port, and clients must be
able to reach it from wherever they operate.

Current Compose behavior in this repo:

- the host forwards UDP ports `51281` through `51289` to the `sensos-wireguard` container

That means:

- any network created with a `wg_port` in that range can work without editing Compose
- automatic port allocation therefore supports at most 9 networks with the default setup
- if you create a network on some other port, clients will not be able to reach
  it until you also update host/container port exposure
- on hosts using `ufw`, you typically also need to allow that UDP range explicitly

Example `ufw` rule:

```sh
sudo ufw allow 51281:51289/udp
```

If you are exposing the enrollment API directly on its default port, the
matching `ufw` rule is:

```sh
sudo ufw allow 8765/tcp
```

## Creating A Network

When a new network is created, the server stores:

- `name`
- `wg_public_ip` as the client-visible WireGuard endpoint address
- `wg_port`
- a generated `10.<hash(name)>.0.0/16` WireGuard address range

Inside that `/16`, `x.y.0.1` is reserved for the API proxy. Client allocation
then picks the next free host address while scanning the `/16` continuously,
starting at `x.y.1.1` by default, while skipping any address ending in `.0` or
`.255`.
If you want clients to begin in `x.y.2.*`, `x.y.3.*`, and so on, start
enrollment with the corresponding `subnet_offset`.
Use `subnet_offset=0` only when you explicitly want automatic allocation in
`x.y.0.*`.

Operationally, this means:

1. the server API must be reachable so the network can be created
2. the selected `wg_public_ip` must be correct from the client's point of view
3. the selected `wg_port` must be open on the Docker host, any firewalls, and any upstream NAT/router

If the `wg_public_ip` or `wg_port` are wrong, enrollment may still succeed, but
the client will not form a working WireGuard tunnel.

This repo now expects networks to be created explicitly after the server is
already running, for example:

```sh
./bin/create-network testing
./bin/create-network biosense
./bin/create-network testing --wg-public-ip server.example.org --wg-port 51820
```

In the first two examples, `bin/create-network` uses the host's detected public
IPv4 address and allocates the next free public WireGuard port in `51281..51289`.
Use `--wg-public-ip` or `--wg-port` when clients should target a different
address, hostname, or port.

If all 9 default WireGuard ports are already assigned, network creation stops.
At that point you need manual intervention: free an existing port, pick an
override port with `--wg-port`, and extend host/container/firewall exposure for
that port.

The server does not automatically create a default network at startup.

## Setup-Time vs Runtime Addressing

The client stores several values in `/sensos/etc/network.conf`:

- `SERVER_PORT`
- `SERVER_WG_IP`
- `WG_ENDPOINT_IP`
- `WG_ENDPOINT_PORT`
- `CLIENT_WG_IP`
- `NETWORK_NAME`

These are not all the same thing.

`SERVER_PORT`:

- the API port used by the client
- initially comes from `config-network --port`
- later used for steady-state API calls

`WG_ENDPOINT_IP` and `WG_ENDPOINT_PORT`:

- the public/reachable WireGuard endpoint returned by the server
- can be overridden at enrollment time with `--wg-endpoint`

`SERVER_WG_IP`:

- the server API address inside the WireGuard network
- currently derived on the client as `x.y.0.1` from the assigned client WireGuard IP

This means the common steady-state path is:

1. client connects WireGuard to `WG_ENDPOINT_IP:WG_ENDPOINT_PORT`
2. client then reaches the API at `http://SERVER_WG_IP:SERVER_PORT`

## Common Failure Modes

### Enrollment works, but the tunnel never comes up

Likely causes:

- wrong `wg_public_ip`
- wrong `wg_port`
- UDP port not exposed on the Docker host
- upstream firewall or NAT not forwarding that UDP port

### Enrollment cannot reach the server at all

Likely causes:

- wrong `--config-server`
- wrong `--port`
- API container not running
- host firewall blocking the API port

### Enrollment works, but later status updates fail

Likely causes:

- client and server API passwords no longer match
- client can reach the setup-time API address but not the WireGuard endpoint
- WireGuard is up, but `SERVER_PORT` is wrong

## QEMU Testing

For two-VM testing on one host:

- server VM helper forwards host `18765` to guest `8765`
- server VM helper forwards host `15182/udp` to guest `51281/udp` by default
- client VM can reach the host as `10.0.2.2`

So from the client VM, a common enrollment target is:

```sh
config-network --config-server 10.0.2.2 --port 18765 --network <network>
```

That setup-time path is separate from the WireGuard endpoint that the server
returns for steady-state operation.
