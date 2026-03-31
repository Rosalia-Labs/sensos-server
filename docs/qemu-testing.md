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

By default, the helper creates two 32 GB qcow2 disks:

- base/system disk: `32G`
- data disk: `32G`

## Workflow

1. Put a Debian ARM64 installer ISO at:

```bash
test/qemu/artifacts/iso/debian-trixie-arm64-netinst.iso
```

2. Create and install the base VM once:

```bash
test/qemu/run-debian-trixie-arm64 install
```

3. After Debian finishes installing and reboots inside QEMU, log in as `root`
   in the VM before quitting QEMU and do the one-time bootstrap work there.

Create the `sensos` user and install the basic tools:

```bash
apt-get update
apt-get install -y git sudo curl docker.io docker-compose
adduser sensos
usermod -aG docker sensos
```

That step is important to do before quitting the install boot. It makes the
user, group membership, and package install part of the persistent base image
instead of something you would lose on later disposable `run` boots.

If you want the base image to keep a repo checkout too, you can also switch to
`sensos` before quitting QEMU and clone the repo there:

```bash
su - sensos
git clone https://github.com/Rosalia-Labs/sensos-server.git
```

That is optional, but it makes the base checkout sticky as part of the installed
image instead of something you recreate later.

4. Log in as `sensos`, clone the repo, configure the server, and start it:

```bash
git clone <repo-url>
cd sensos-server
./bin/configure-server.sh
./bin/start-server.sh
```

If you also want reboot persistence inside the guest, have a privileged user
install the optional systemd unit:

```bash
./install
sudo systemctl start sensos-server
```

5. Once the base image is set up the way you want, shut down the guest cleanly,
   exit QEMU, and use disposable run boots when you want a non-sticky test session:

```bash
test/qemu/run-debian-trixie-arm64 run
```

The `run` command uses `-snapshot`, so guest disk changes are discarded when
QEMU exits.

## Connectivity

The script forwards host port `2223` to guest SSH:

```bash
ssh -p 2223 <user>@127.0.0.1
```

It also forwards the server API back to the host:

- API: `127.0.0.1:18765 -> guest:8765`

This makes two-VM testing practical:

1. Run the server VM with this helper.
2. Run the client VM with the client helper.
3. In the client VM, point `config-network` at `10.0.2.2 --port 18765`.

With QEMU user networking, each guest can usually reach macOS-hosted services at:

```text
10.0.2.2
```

From the client VM, `10.0.2.2:18765` reaches the server API forwarded from the
server VM through the macOS host.

## Installer display

The launcher attaches a virtio GPU plus USB keyboard and tablet so the Debian
installer appears in the QEMU window on macOS. If you ever land in the QEMU
monitor instead of the guest display, try:

```text
Ctrl-Alt-1
```

to switch back to the guest console.
