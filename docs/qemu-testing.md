# QEMU Testing

QEMU testing documentation for this repo lives under:

- [`test/qemu/docs/README.md`](../test/qemu/docs/README.md)

That guide covers the local Apple Silicon and MacPorts-based Debian Trixie ARM64
workflow, including:

- VM artifact layout
- initial install versus disposable `run` boots, including the requirement to
  cleanly shut down the guest at the end of install so changes persist
- guest bootstrap and update flows
- server bring-up, network creation, and endpoint reconciliation in QEMU
- host forwards for API, dashboard, and SSH
- client enrollment against the QEMU-forwarded setup API and published
  WireGuard endpoint
- post-enrollment verification from the server guest and Docker containers
