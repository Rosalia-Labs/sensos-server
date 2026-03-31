FROM postgres:17-bookworm

# Install the PostgreSQL 17 PostGIS extension files that match the base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-17-postgis-3 \
    postgresql-17-postgis-3-scripts \
    && test -f /usr/share/postgresql/17/extension/postgis.control \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["postgres"]
