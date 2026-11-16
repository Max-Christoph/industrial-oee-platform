#!/usr/bin/env bash
# Preflight for 24/7 rootless Podman: containers must survive logout/reboot.
set -euo pipefail

COMPOSE_CMD="${COMPOSE:-podman compose}"
fail=0

if ! command -v podman >/dev/null 2>&1; then
  echo "ERROR: podman not found. Install podman (>= 4.9) and podman-compose or docker-compose." >&2
  exit 1
fi

if ! $COMPOSE_CMD version >/dev/null 2>&1; then
  echo "ERROR: '$COMPOSE_CMD' does not work. Install podman-compose (pip/dnf/apt) or docker-compose." >&2
  fail=1
fi

if [ "$(id -u)" -eq 0 ]; then
  echo "WARNING: running as root - this stack is designed for rootless Podman."
elif command -v loginctl >/dev/null 2>&1; then
  linger="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo unknown)"
  if [ "$linger" != "yes" ]; then
    echo "ERROR: lingering is disabled for $USER - rootless containers stop at logout/reboot." >&2
    echo "       Fix: sudo loginctl enable-linger $USER" >&2
    [ "${SKIP_LINGER_CHECK:-0}" = "1" ] || fail=1
  fi
else
  echo "WARNING: loginctl not available - cannot verify lingering."
fi

exit $fail
