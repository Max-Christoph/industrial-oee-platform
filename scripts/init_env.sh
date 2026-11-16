#!/usr/bin/env bash
# Creates .env from .env.example and replaces every __GENERATE__ with a random secret.
# Never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  echo ".env already exists - leaving it untouched."
  exit 0
fi

gen() { openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

umask 077
: > .env
while IFS= read -r line || [ -n "$line" ]; do
  while [[ "$line" == *__GENERATE__* ]]; do
    line="${line/__GENERATE__/$(gen)}"
  done
  printf '%s\n' "$line" >> .env
done < .env.example
chmod 600 .env
echo ".env created with random secrets (mode 600)."
