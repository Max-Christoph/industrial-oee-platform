#!/usr/bin/env bash
# TLS material for PostgreSQL and Mosquitto in site_overrides/certs/:
#   * If server.crt + server.key already exist (e.g. from your corporate PKI), nothing happens.
#   * Otherwise a private CA and a server certificate (10 years) are generated.
# Optional: ca.crt (chain of your PKI) - recommended for clients (Power BI, devices).
set -euo pipefail
export MSYS_NO_PATHCONV=1  # Git-Bash on Windows: keep "/CN=..." literal
cd "$(dirname "$0")/.."

DIR=site_overrides/certs
DAYS=3650
mkdir -p "$DIR"

if [ -s "$DIR/server.crt" ] && [ -s "$DIR/server.key" ]; then
  echo "Using existing certificates in $DIR (corporate PKI or previously generated)."
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

HOSTS="${CERT_HOSTNAMES:-}"
if [ -z "$HOSTS" ] && [ -f .env ]; then
  HOSTS="$(grep -E '^CERT_HOSTNAMES=' .env | cut -d= -f2- || true)"
fi
HOSTS="localhost,127.0.0.1,timescaledb,mosquitto,$(hostname 2>/dev/null || echo oee-host),${HOSTS}"

SAN=""
IFS=',' read -ra entries <<< "$HOSTS"
declare -A seen=()
for h in "${entries[@]}"; do
  h="$(echo "$h" | xargs)"
  [ -z "$h" ] && continue
  [ -n "${seen[$h]:-}" ] && continue
  seen[$h]=1
  if [[ "$h" =~ ^[0-9.]+$ || "$h" == *:* ]]; then SAN+="IP:$h,"; else SAN+="DNS:$h,"; fi
done
SAN="${SAN%,}"

umask 077
TMP="$(mktemp -d "$DIR/.tmp.XXXXXX")"  # relative path: also works with native openssl on Windows
trap 'rm -rf "$TMP"' EXIT

openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days "$DAYS" \
  -subj "/CN=industrial-oee-platform local CA" \
  -keyout "$TMP/ca.key" -out "$DIR/ca.crt" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"

openssl req -newkey rsa:3072 -sha256 -nodes -subj "/CN=oee-platform" \
  -keyout "$DIR/server.key" -out "$TMP/server.csr"

printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$SAN" > "$TMP/ext.cnf"
openssl x509 -req -sha256 -days "$DAYS" -in "$TMP/server.csr" -CA "$DIR/ca.crt" -CAkey "$TMP/ca.key" \
  -CAcreateserial -out "$DIR/server.crt" -extfile "$TMP/ext.cnf"

chmod 600 "$DIR/server.key"
chmod 644 "$DIR/server.crt" "$DIR/ca.crt"
echo "Self-signed certificates created in $DIR (valid $DAYS days, SAN: $SAN)."
echo "Distribute $DIR/ca.crt to clients (Power BI / devices) to trust the server."
