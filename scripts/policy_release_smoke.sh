#!/usr/bin/env bash
# Run a disposable TLS production-profile check with synthetic documents.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
umask 077

scratch="$(mktemp -d)"
project="jianshu-policy-check-$$"
worker="${project}-worker:local"
http_port="${RAG_POLICY_SMOKE_HTTP_PORT:-29080}"
https_port="${RAG_POLICY_SMOKE_HTTPS_PORT:-29443}"
socket="${RAG_DOCKER_SOCKET:-/var/run/docker.sock}"
if [[ -z "${RAG_DOCKER_SOCKET:-}" && ! -S "$socket" ]]; then
  endpoint="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}')"
  if [[ "$endpoint" == unix://* ]]; then socket="${endpoint#unix://}"; fi
fi
compose=(docker compose --project-name "$project" --env-file "$scratch/environment"
  -f compose.yaml -f compose.production.yaml)
cleanup() {
  result=$?
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  docker image rm "$worker" >/dev/null 2>&1 || true
  rm -rf "$scratch"
  exit "$result"
}
trap cleanup EXIT

password_a="$(openssl rand -hex 24)"
password_b="$(openssl rand -hex 24)"
hash_password() {
  docker run --rm caddy:2.11.9-alpine caddy hash-password --plaintext "$1"
}
printf 'alice %s\nbob %s\n' "$(hash_password "$password_a")" "$(hash_password "$password_b")" > "$scratch/users"
{
  printf 'RAG_DOMAIN=localhost\nRAG_GATEWAY_BIND=127.0.0.1\nRAG_HTTP_PORT=%s\nRAG_HTTPS_PORT=%s\n' "$http_port" "$https_port"
  printf 'RAG_GATEWAY_KEY=%s\nRAG_USERS_FILE=%s\n' "$(openssl rand -hex 32)" "$scratch/users"
  printf 'RAG_SANDBOX_IMAGE=%s\nRAG_DOCKER_SOCKET=%s\n' "$worker" "$socket"
  printf 'LLM_API_KEY=\nRAG_LANGSMITH_EXPORT=false\nRAG_POSTGRES_HOST=\n'
} > "$scratch/environment"
unset RAG_DOMAIN RAG_GATEWAY_BIND RAG_HTTP_PORT RAG_HTTPS_PORT RAG_GATEWAY_KEY RAG_USERS_FILE RAG_SANDBOX_IMAGE
export LLM_API_KEY="" RAG_LANGSMITH_EXPORT=false RAG_POSTGRES_HOST=""
"${compose[@]}" up -d --build --wait
"${compose[@]}" exec -T gateway cat /data/caddy/pki/authorities/local/root.crt > "$scratch/ca.crt"

RAG_SMOKE_TLS_PORT="$https_port" RAG_SMOKE_CA="$scratch/ca.crt" \
  RAG_SMOKE_ALICE="$password_a" RAG_SMOKE_BOB="$password_b" \
  "${RAG_POLICY_SMOKE_PYTHON:-python3}" scripts/check_policy_release.py
"${compose[@]}" restart api
"${compose[@]}" up -d --wait
RAG_SMOKE_TLS_PORT="$https_port" RAG_SMOKE_CA="$scratch/ca.crt" \
  RAG_SMOKE_ALICE="$password_a" RAG_SMOKE_BOB="$password_b" \
  "${RAG_POLICY_SMOKE_PYTHON:-python3}" scripts/check_policy_release.py --after-restart
