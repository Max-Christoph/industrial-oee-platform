#!/bin/sh
# Copies the mounted TLS material (site_overrides/certs) into a postgres-owned
# location with the strict permissions PostgreSQL requires, then hands over to
# the stock entrypoint. Works with rootless Podman (bind mounts are owned by the
# mapped host user and are not chown-able in place).
set -eu

SRC=/certs
DST=/var/lib/postgresql/certs

if [ ! -s "$SRC/server.crt" ] || [ ! -s "$SRC/server.key" ]; then
  echo "FATAL: $SRC/server.crt or server.key missing. Run 'make certs' (self-signed) or place your PKI certificates in site_overrides/certs/." >&2
  exit 1
fi

mkdir -p "$DST"
cp "$SRC/server.crt" "$DST/server.crt"
cp "$SRC/server.key" "$DST/server.key"
if [ -s "$SRC/ca.crt" ]; then cp "$SRC/ca.crt" "$DST/ca.crt"; fi
chown -R postgres:postgres "$DST"
chmod 0700 "$DST"
chmod 0644 "$DST/server.crt"
chmod 0600 "$DST/server.key"

exec docker-entrypoint.sh "$@"
