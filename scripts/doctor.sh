#!/usr/bin/env bash
# `make doctor` - checks the host for everything this project needs and prints
# copy-paste fixes for every problem. Exit code 1 if anything blocks `make up`.
set -uo pipefail
cd "$(dirname "$0")/.."

COMPOSE_CMD="${COMPOSE:-podman compose}"
if [ -t 1 ]; then G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'; else G=; Y=; R=; B=; N=; fi
fails=0; warns=0

ok()   { printf '  %s✔%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; [ -n "${2:-}" ] && printf '      fix: %s%s%s\n' "$B" "$2" "$N"; warns=$((warns + 1)); }
fail() { printf '  %s✘%s %s\n' "$R" "$N" "$1"; [ -n "${2:-}" ] && printf '      fix: %s%s%s\n' "$B" "$2" "$N"; fails=$((fails + 1)); }
has()  { command -v "$1" >/dev/null 2>&1; }
envval() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }

pkg_hint() {
  if has dnf; then echo "sudo dnf install -y $1"
  elif has apt-get; then echo "sudo apt-get update && sudo apt-get install -y $1"
  elif has zypper; then echo "sudo zypper install -y $1"
  else echo "install '$1' with your package manager"; fi
}

echo "${B}Container runtime${N}"
if has podman; then
  ok "podman $(podman --version 2>/dev/null | awk '{print $3}')"
  rootless="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo unknown)"
  if [ "$rootless" = "true" ]; then ok "podman runs rootless"
  else warn "podman is not running rootless (got: $rootless)" "run make as your normal user, not with sudo"; fi
else
  fail "podman not found" "$(pkg_hint podman)"
fi
if $COMPOSE_CMD version >/dev/null 2>&1; then
  ok "'$COMPOSE_CMD' works"
else
  fail "'$COMPOSE_CMD' does not work" "$(pkg_hint podman-compose)   # or: uv tool install podman-compose"
fi

echo "${B}24/7 operation${N}"
if [ "$(id -u)" -eq 0 ]; then
  warn "running as root - the stack is designed for rootless Podman" "use a dedicated non-root user"
elif has loginctl; then
  linger="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo unknown)"
  if [ "$linger" = "yes" ]; then ok "lingering enabled for $USER (containers survive logout/reboot)"
  else fail "lingering disabled for $USER - containers stop at logout" "sudo loginctl enable-linger $USER"; fi
else
  warn "loginctl not available - cannot check lingering (systemd host required for 24/7)"
fi
if has getenforce; then
  ok "SELinux: $(getenforce) (bind mounts use :z / :ro,z)"
fi

echo "${B}Tools${N}"
if has uv; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
  if uv python find '>=3.12' >/dev/null 2>&1; then ok "Python >= 3.12 available to uv"
  else warn "no Python >= 3.12 found yet" "uv python install 3.12"; fi
else
  fail "uv not found (needed for make test / make dev)" "curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
for tool in make openssl; do
  if has "$tool"; then ok "$tool"; else fail "$tool not found" "$(pkg_hint "$tool")"; fi
done

echo "${B}Configuration${N}"
if [ -f .env ]; then
  if grep -q "__GENERATE__" .env; then fail ".env contains unreplaced __GENERATE__ placeholders" "rm .env && make env"
  else ok ".env present"; fi
  perms="$(stat -c '%a' .env 2>/dev/null || echo '?')"
  case "$perms" in 600|400) ok ".env permissions $perms" ;; '?') ;; *) warn ".env is readable by others ($perms)" "chmod 600 .env" ;; esac
else
  fail ".env missing" "make env"
fi
if [ -s site_overrides/certs/server.crt ] && [ -s site_overrides/certs/server.key ]; then
  exp="$(openssl x509 -in site_overrides/certs/server.crt -noout -enddate 2>/dev/null | cut -d= -f2)"
  if openssl x509 -in site_overrides/certs/server.crt -noout -checkend $((30 * 86400)) >/dev/null 2>&1; then
    ok "TLS certificate present (valid until $exp)"
  else
    fail "TLS certificate expires within 30 days or is invalid ($exp)" "replace site_overrides/certs/server.* (or delete them and run: make certs)"
  fi
else
  warn "no TLS certificates yet (generated automatically by make up)" "make certs"
fi
for f in oee_rules.yaml assets.csv custom_views.sql; do
  if [ -f "site_overrides/$f" ]; then ok "site_overrides/$f (your plant)"; else ok "$f -> demo plant default"; fi
done

echo "${B}Ports${N}"
running="$($COMPOSE_CMD ps -q 2>/dev/null | wc -l | tr -d ' ')"
port_in_use() {
  if has ss; then ss -ltnH "sport = :$1" 2>/dev/null | grep -q .
  else (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; fi
}
for spec in "PG_PORT:5432:PostgreSQL" "MQTT_TLS_PORT:8883:MQTT TLS" "API_PORT:8000:API" "MCP_PORT:8001:MCP" "GRAFANA_PORT:3000:Grafana" "MQTT_DEV_PORT:1883:MQTT dev (make dev)"; do
  IFS=: read -r var def label <<< "$spec"
  port="$(envval "$var")"; port="${port:-$def}"
  if port_in_use "$port"; then
    if [ "${running:-0}" -gt 0 ]; then ok "$label port $port in use (stack is running)"
    else fail "$label port $port is already used by another program" "set $var=<free port> in .env  (see: ss -ltnp | grep :$port)"; fi
  else
    ok "$label port $port free"
  fi
done

echo
if [ "$fails" -gt 0 ]; then
  printf '%s%d problem(s)%s, %d warning(s) - fix the commands above, then run "make doctor" again.\n' "$R" "$fails" "$N" "$warns"
  exit 1
fi
printf '%sAll good%s (%d warning(s)). Next: make up   (or: make dev)\n' "$G" "$N" "$warns"
