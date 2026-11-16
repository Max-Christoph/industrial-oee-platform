#!/bin/sh
# Builds password file, ACL and TLS material inside the data volume with the
# ownership/permissions Mosquitto expects, then starts the broker.
set -eu

DATA=/mosquitto/data
CERTS="$DATA/certs"
: "${MQTT_INGEST_USER:?}" "${MQTT_INGEST_PASSWORD:?}" "${MQTT_DEVICE_USER:?}" "${MQTT_DEVICE_PASSWORD:?}"

if [ ! -s /certs/server.crt ] || [ ! -s /certs/server.key ]; then
  echo "FATAL: /certs/server.crt or server.key missing. Run 'make certs' or provide PKI certificates in site_overrides/certs/." >&2
  exit 1
fi

mkdir -p "$CERTS"
cp /certs/server.crt "$CERTS/server.crt"
cp /certs/server.key "$CERTS/server.key"
if [ -s /certs/ca.crt ]; then
  cp /certs/ca.crt "$CERTS/ca.crt"
else
  # Corporate certs without separate CA bundle: the server chain serves as CA file.
  cp /certs/server.crt "$CERTS/ca.crt"
fi

rm -f "$DATA/passwd"
touch "$DATA/passwd"
chmod 0600 "$DATA/passwd"
mosquitto_passwd -b "$DATA/passwd" "$MQTT_INGEST_USER" "$MQTT_INGEST_PASSWORD"
mosquitto_passwd -b "$DATA/passwd" "$MQTT_DEVICE_USER" "$MQTT_DEVICE_PASSWORD"

sed -e "s/__DEVICE_USER__/$MQTT_DEVICE_USER/" -e "s/__INGEST_USER__/$MQTT_INGEST_USER/" \
  /mosquitto/config/acl > "$DATA/acl"

chown -R mosquitto:mosquitto "$DATA"
chmod 0700 "$CERTS"
chmod 0600 "$CERTS/server.key" "$DATA/acl"

# Use the generated ACL from the data volume.
sed "s#^acl_file .*#acl_file $DATA/acl#" /mosquitto/config/mosquitto.conf > "$DATA/mosquitto.conf"
chown mosquitto:mosquitto "$DATA/mosquitto.conf"

exec /docker-entrypoint.sh /usr/sbin/mosquitto -c "$DATA/mosquitto.conf"
