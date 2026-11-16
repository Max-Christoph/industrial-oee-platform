#!/bin/bash
# Enforce TLS for every non-loopback connection (Power BI / corporate BI require SSL).
# Set PG_SSL_ONLY_REMOTE=false in .env to additionally allow plaintext (not recommended).
set -euo pipefail

HBA="$PGDATA/pg_hba.conf"
cat > "$HBA" <<'EOF'
# TYPE   DATABASE  USER  ADDRESS        METHOD
local    all       all                  trust
host     all       all   127.0.0.1/32   scram-sha-256
host     all       all   ::1/128        scram-sha-256
hostssl  all       all   0.0.0.0/0      scram-sha-256
hostssl  all       all   ::/0           scram-sha-256
EOF

if [ "${PG_SSL_ONLY_REMOTE:-true}" != "true" ]; then
  echo "host     all       all   all            scram-sha-256" >> "$HBA"
fi
echo "pg_hba.conf written (ssl only remote: ${PG_SSL_ONLY_REMOTE:-true})"
