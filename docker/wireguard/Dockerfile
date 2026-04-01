FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wireguard-tools \
    iputils-ping \
    iproute2 \
    python3 \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
COPY reconcile.py /reconcile.py
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
