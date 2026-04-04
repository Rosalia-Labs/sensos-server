# Container Control Plane

This page documents the current server container architecture after the move
away from shared-volume polling and the old local state-machine approach.

The controlling idea is:

- the PostgreSQL database is the control plane
- each WireGuard-capable container owns its own private key material
- only public keys and runtime status are exchanged between containers

This is a local container orchestration design. It does not change the
client-server schema that clients depend on.

## Container Roles

The Compose stack currently runs four main containers:

- `sensos-database`: PostgreSQL state store and control plane
- `sensos-controller`: FastAPI control service and schema bootstrap
- `sensos-wireguard`: server-side WireGuard reconciler
- `sensos-api-proxy`: nginx API proxy plus proxy-side WireGuard reconciler

## Design Summary

The controller no longer writes WireGuard config into a shared volume, does not
manage Docker through the socket, and does not generate private keys for other
containers.

Instead:

- the controller creates and updates the database schema
- `sensos-wireguard` reads desired network/peer state from the database
- `sensos-api-proxy` reads desired network state from the database
- each reconciler renders its own local config from database state
- each reconciler stores its own private key in its own private state volume
- each reconciler publishes only its public key and runtime status back into the database

## Data Ownership

The existing client-facing tables remain the source of truth for network and
peer identity:

- `sensos.networks`
- `sensos.wireguard_peers`
- `sensos.wireguard_keys`
- related client status / SSH / hardware / location tables

The new container orchestration status is tracked in:

- `sensos.runtime_wireguard_status`

That table is used for runtime visibility and coordination. It is not intended
to replace the network and peer tables.

## Private Key Boundaries

Private key handling now follows strict local ownership:

- `sensos-wireguard` generates and stores server private keys under its local state volume
- `sensos-api-proxy` generates and stores proxy private keys under its local state volume
- the controller does not generate or persist WireGuard private keys for those containers
- the database stores public keys only

Current reserved addresses inside each network:

- `.1`: API proxy WireGuard address
- the rest of `x.y.0.*` is available for infrastructure-style assignments if you
  manage them explicitly
- automatic client peer allocation reserves `.1` and then assigns the next free
  host address while scanning the `/16` continuously from the selected starting
  point, skipping any address ending in `.0` or `.255`:
  `x.y.1.1`, `x.y.1.2`, ..., `x.y.1.254`, `x.y.2.1`, ...
- enrollment can override that starting `/24` via `subnet_offset`; use
  `subnet_offset=0` if you explicitly want automatic allocation in `x.y.0.*`

## Reconciliation Model

The current reconciliation flow is:

1. the controller starts and ensures the `sensos` schema exists
2. the controller reports healthy on `GET /healthz` only after schema/bootstrap work completes
3. Compose waits for that controller health check before starting `sensos-wireguard` and `sensos-api-proxy`
4. `bin/create-network` creates a row in `sensos.networks`
5. `sensos-wireguard` sees the new network in the database
6. `sensos-wireguard` generates or reuses its private key, derives a public key, and updates `sensos.networks.wg_public_key`
7. `sensos-api-proxy` sees the network public key and reconciles its own local WireGuard interface
8. `sensos-api-proxy` ensures its peer row and active public key exist in `sensos.wireguard_peers` and `sensos.wireguard_keys`
9. both reconcilers publish status rows into `sensos.runtime_wireguard_status`

Peer registration is separate:

1. a client calls `register-peer`
2. the server allocates a client WireGuard IP and returns network connection details
3. the client later calls `register-wireguard-key`
4. `sensos-wireguard` picks up the new active peer public key from the database and reconciles the server interface

Peer lifecycle is also explicit:

- mark a peer inactive when it still matters in inventory but should keep its
  assigned IP reserved
- delete a peer only when it should be fully purged; that removes the peer row
  and makes the WireGuard IP available for reuse

## Runtime State Volumes

The old shared coordination volume has been removed.

The remaining state is local to the owning container:

- `sensos_database`: PostgreSQL data
- `sensos_wireguard_state`: server reconciler private/rendered WireGuard state
- `sensos_api_proxy_state`: proxy reconciler private/rendered WireGuard state

This keeps secret material local to the container that owns it.

## Privilege Model

The current privilege split is:

- `sensos-controller`: no WireGuard capability, no Docker socket, no privileged mode
- `sensos-wireguard`: `NET_ADMIN` only
- `sensos-api-proxy`: `NET_ADMIN` only
- `sensos-database`: normal database container privileges

This is a deliberate step toward reducing host-level privilege usage and
removing broad cross-container control.

## API Access Split

The controller API now has three access classes:

- `GET /healthz` is unauthenticated and exists only for local orchestration readiness checks
- client-operational routes accept either `CLIENT_API_PASSWORD` or `ADMIN_API_PASSWORD`
- operator and debugging routes require `ADMIN_API_PASSWORD`

This keeps client enrollment and telemetry separate from the more sensitive
operator surface such as peer deletion, network creation, and database
inspection.

## Operational Checks

Useful checks while bringing the server up:

```sh
docker ps
docker logs --tail=100 sensos-controller
docker logs --tail=100 sensos-wireguard
docker logs --tail=100 sensos-api-proxy
```

API-level checks:

```sh
source docker/.env
curl -u "sensos:$ADMIN_API_PASSWORD" http://127.0.0.1:8765/get-network-info?network_name=<network>
curl -u "sensos:$ADMIN_API_PASSWORD" http://127.0.0.1:8765/wireguard-status
curl -u "sensos:$ADMIN_API_PASSWORD" http://127.0.0.1:8765/inspect-database?limit=20
```

Container WireGuard checks:

```sh
docker exec sensos-wireguard wg show
docker exec sensos-api-proxy wg show
```

Database runtime-status check:

```sh
source docker/.env
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" sensos-database \
  psql -U postgres -d postgres -c \
  "select n.name, r.component, r.role, r.status, r.public_key, r.last_error, r.updated_at
   from sensos.networks n
   left join sensos.runtime_wireguard_status r on r.network_id = n.id
   order by n.name, r.component;"
```

Expected healthy result:

- network exists in `sensos.networks`
- `sensos.networks.wg_public_key` is populated
- runtime rows exist for `sensos-wireguard` and `sensos-api-proxy`
- both runtime rows report `status = ready`

## QEMU Notes

When commands are run inside the QEMU server guest:

- use the guest-local API port, usually `127.0.0.1:8765`
- use the host-reachable WireGuard endpoint values that client guests need

Typical server-guest network creation:

```sh
./bin/create-network testing \
  --config-server 127.0.0.1 \
  --port 8765 \
  --wg-public-ip 10.0.2.2 \
  --wg-port 51281
```

Typical client-guest enrollment:

```sh
config-network --config-server 10.0.2.2 --port 18765 --network testing
```

## Transition Items Still Open

- Compose still exposes a fixed UDP range for WireGuard ports instead of deriving host exposure from defined networks
- the runtime status model is intentionally minimal and does not yet capture richer reconciliation intent/history
- the backup path and backup docs have not yet been fully reworked around the new per-container private-key ownership model
- automated integration coverage for the new DB-backed reconciliation flow is still incomplete
- the client-side steady-state assumptions should be revalidated against the current address allocation model where `.1` is the API proxy, automatic client allocation defaults to `x.y.1.1+`, and `x.y.0.*` can still be used intentionally for infrastructure assignments
