# Server User Setup

This repo is intended to run from a normal user-owned checkout instead of from
`root`.

Typical pattern:

- SSH to the host as your normal admin account
- use `sudo` from that admin account to create and manage the service user
- use the service user only for the repo checkout and normal runtime commands

Example below uses a dedicated service account named `sensos` on a Debian-family
server. Adjust the username and repo URL if needed.

## Create the User

From your normal admin login, create the account with no password set:

```sh
sudo adduser --disabled-password --gecos "" sensos
sudo passwd -l sensos
```

That leaves the account unavailable for password login. Use SSH keys instead if
you want direct SSH access to the service account at all.

## Install SSH Keys

Create the SSH directory and install an authorized key for the new user:

```sh
sudo install -d -m 700 -o sensos -g sensos /home/sensos/.ssh
sudo install -m 600 -o sensos -g sensos /tmp/sensos.authorized_keys /home/sensos/.ssh/authorized_keys
```

Replace `/tmp/sensos.authorized_keys` with a file containing the public key or
keys that should be allowed to log in as `sensos`.

## Add Docker Access

Ensure the `docker` group exists and add the user to it:

```sh
sudo groupadd -f docker
sudo usermod -aG docker sensos
```

The user must log out and back in before the new group membership applies.

Note: membership in the `docker` group is effectively privileged on the host.
Use a dedicated service user and only trusted SSH keys.

## Clone the Repo as That User

From your admin login, clone the repo as that user without switching your main
SSH session:

```sh
sudo -u sensos -H git clone https://github.com/Rosalia-Labs/sensos-server.git /home/sensos/sensos-server
```

Or switch into the account first:

```sh
su - sensos
git clone https://github.com/Rosalia-Labs/sensos-server.git
```

## Next Steps

After the service-user bootstrap is complete, continue with the canonical
bring-up sequence in [Getting started](getting-started.md).

If you also want the optional systemd service, run it from your normal admin
SSH login with `sudo` against the service user's checkout:

```sh
sudo /home/sensos/sensos-server/bin/install-service
sudo systemctl start sensos-server
```

`bin/install-service` will install the unit against that checkout and use the
checkout owner as the default `User=` for the service. The normal repo checkout
and runtime should still stay owned by the service user. If the privileged
setup step fails, `bin/install-service` now prints a hint to rerun it from a
privileged account.
