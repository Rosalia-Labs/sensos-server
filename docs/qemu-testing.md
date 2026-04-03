# QEMU Testing

This repo includes a local helper for running a Debian Trixie ARM64 VM for the
SensOS server on Apple Silicon with MacPorts QEMU.

Primary launcher:

- [`test/qemu/run-debian-trixie-arm64`](../test/qemu/run-debian-trixie-arm64)

## Artifacts

VM artifacts live under:

`test/qemu/artifacts/`

That path is gitignored.

Layout:

- `test/qemu/artifacts/images/debian-trixie-arm64-base.qcow2`
- `test/qemu/artifacts/images/debian-trixie-arm64-data.qcow2`
- `test/qemu/artifacts/images/edk2-arm64-vars.fd`
- `test/qemu/artifacts/iso/debian-trixie-arm64-netinst.iso`

The current helper creates two 32 GB qcow2 disks:

- base/system disk: `32G`
- data disk: `32G`

Current note:

- the server QEMU environment does not actually need the second data disk
- the extra disk is only needed for the client-side QEMU environment
- the server helper still creates and attaches the data disk today, but that is not a server runtime requirement

## Workflow

1. Put a Debian ARM64 installer ISO at:

```bash
test/qemu/artifacts/iso/debian-trixie-arm64-netinst.iso
```

2. Create and install the base VM once:

```bash
test/qemu/run-debian-trixie-arm64 install
```

3. After Debian finishes installing and reboots inside QEMU, log in to the VM,
   switch to `root`, and run the guest bootstrap script before quitting QEMU.
   On Debian that usually means `su -` with the root password you set during
   install, or `su -c '<command>'`.

```bash
su -
apt-get update
apt-get install -y curl
curl -fsSL https://raw.githubusercontent.com/Rosalia-Labs/sensos-server/main/test/qemu/bootstrap-debian-server | bash
```

That script installs the Debian packages needed to host the server and ensures
the `sensos` user exists for the Docker runtime path. It must be run as
`root`. Use a separate admin account for `sudo` and other privileged host
actions.

Important current QEMU note:

- do not rely on a persistent git checkout in the guest image or on the qcow2
  overlay disk
- in practice, git works poorly there
- clone the repo in each disposable `run` boot instead

Important current packaging note for Debian trixie:

- install both `docker.io` and `docker-compose`
- do not assume the Compose plugin is present just because Docker is installed
- this repo currently expects `docker-compose` to be available in the guest

If you already have the repo in the guest by some other path, you can run the
same script locally instead:

```bash
./test/qemu/bootstrap-debian-server
```

Do that before quitting the install boot so the user setup and package install
become part of the persistent base image. Do not treat the repo checkout as
part of that persistent base image; clone it later during disposable `run`
boots.

4. Once the base image is set up the way you want, shut down the guest cleanly,
   exit QEMU, and use disposable run boots when you want a non-sticky test session:

```bash
test/qemu/run-debian-trixie-arm64 run
```

The `run` command uses `-snapshot`, so guest disk changes are discarded when
QEMU exits.

5. In each disposable `run` boot, clone the repo, configure the server, and
   start it:

```bash
rm -rf ~/sensos-server
git clone https://github.com/Rosalia-Labs/sensos-server.git ~/sensos-server
cd sensos-server
./bin/configure-server
./bin/start-server
```

If you also want reboot persistence inside the guest, have a privileged user
install the optional systemd unit:

```bash
./bin/install-service
su -c 'systemctl start sensos-server'
```

## Validated Test Procedure

The following procedure has been validated for a server VM and a separate client
VM running on one macOS host with QEMU user networking.

### Server VM bring-up

Inside the server guest:

```bash
rm -rf ~/sensos-server
git clone https://github.com/Rosalia-Labs/sensos-server.git ~/sensos-server
cd ~/sensos-server
./bin/configure-server
./bin/start-server
```

Create the test network from inside the server guest:

```bash
./bin/create-network testing \
  --config-server 127.0.0.1 \
  --port 8765 \
  --wg-public-ip 10.0.2.2 \
  --wg-port 15182
```

This is the correct split for the server guest:

- use `127.0.0.1:8765` for the local API call inside the guest
- publish `10.0.2.2:15182` as the WireGuard endpoint that client guests can reach via the macOS host

### Server verification

Inside the server guest, the following checks should pass:

```bash
source docker/.env
curl -u "sensos:$ADMIN_API_PASSWORD" http://127.0.0.1:8765/get-network-info?network_name=testing
curl -u "sensos:$ADMIN_API_PASSWORD" http://127.0.0.1:8765/wireguard-status
docker exec sensos-wireguard wg show
docker exec sensos-api-proxy wg show
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" sensos-database \
  psql -U postgres -d postgres -c \
  "select n.name, r.component, r.status, r.last_error
   from sensos.networks n
   left join sensos.runtime_wireguard_status r on r.network_id = n.id
   order by n.name, r.component;"
```

Healthy result:

- the `testing` network exists
- `sensos-wireguard` and `sensos-api-proxy` both report `status = ready`
- `last_error` is empty for both runtime rows
- `wg show` in both containers shows a live `testing` interface

### Client VM enrollment

Inside a separate client guest:

```bash
config-network --config-server 10.0.2.2 --port 18765 --network testing
```

This is the correct split for the client guest:

- `10.0.2.2:18765` reaches the server API through the macOS host forward
- the returned WireGuard endpoint `10.0.2.2:15182` also reaches the server through the macOS host forward

Validated outcome:

- peer registration succeeded
- client public-key registration succeeded
- the client wrote `/etc/wireguard/testing.conf`
- the client completed SSH key exchange and local setup

### Post-enrollment verification

Back in the server guest:

```bash
source docker/.env
curl -u "sensos:$ADMIN_API_PASSWORD" http://127.0.0.1:8765/wireguard-status
docker exec sensos-wireguard wg show
docker exec sensos-api-proxy wg show
```

Expected result:

- the server-side `testing` interface shows the client peer
- the proxy-side `testing` interface remains up
- handshakes and transfer counters appear after the client brings up WireGuard

## Connectivity

The script forwards host port `2223` to guest SSH:

```bash
ssh -p 2223 <user>@127.0.0.1
```

It also forwards the server API back to the host:

- API: `127.0.0.1:18765 -> guest:8765`
- WireGuard UDP: `127.0.0.1:15182/udp -> guest:51281/udp`

This makes two-VM testing practical:

1. Run the server VM with this helper.
2. Run the client VM with the client helper.
3. In the server VM, create the test network with `wg_public_ip=10.0.2.2` and `wg_port=15182` so the client sees the macOS host as the forwarded WireGuard endpoint.
4. In the client VM, point `config-network` at `10.0.2.2 --port 18765`.

With QEMU user networking, each guest can usually reach macOS-hosted services at:

```text
10.0.2.2
```

From the client VM, `10.0.2.2:18765` reaches the server API forwarded from the
server VM through the macOS host.

For the WireGuard tunnel, the same host IP works: client traffic to
`10.0.2.2:15182/udp` is forwarded by macOS into the server VM's WireGuard port
on `51281/udp`.

## Installer display

The launcher attaches a virtio GPU plus USB keyboard and tablet so the Debian
installer appears in the QEMU window on macOS. If you ever land in the QEMU
monitor instead of the guest display, try:

```text
Ctrl-Alt-1
```

to switch back to the guest console.
