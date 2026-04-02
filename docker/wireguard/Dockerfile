FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wireguard-tools \
    iputils-ping \
    iproute2 \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages psycopg[binary]

COPY entrypoint.sh /entrypoint.sh
COPY reconcile.py /reconcile.py
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
