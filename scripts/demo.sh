#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
mode="${1:-start}"
if [[ $# -gt 1 ]]; then
    printf '用法：bash scripts/demo.sh [start|live|stop]\n' >&2
    exit 2
fi

env_file=/dev/null
case "$mode" in
    start|stop) export LLM_API_KEY="" ;;
    live)
        env_file="${RAG_DEMO_ENV_FILE:-.env}"
        if [[ ! -f "$env_file" ]]; then
            printf '请先配置模型地址和密钥。可通过 RAG_DEMO_ENV_FILE 指定配置文件。\n' >&2
            exit 1
        fi
        ;;
    *)
        printf '用法：bash scripts/demo.sh [start|live|stop]\n' >&2
        exit 2
        ;;
esac

export RAG_API_PORT="${RAG_DEMO_API_PORT:-28000}"
export RAG_UI_PORT="${RAG_DEMO_UI_PORT:-28501}"
export RAG_SANDBOX_IMAGE=jianshu-react-demo-worker:local
if [[ -z "${RAG_DOCKER_SOCKET:-}" && ! -S /var/run/docker.sock ]]; then
    endpoint="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}' 2>/dev/null || true)"
    if [[ "$endpoint" == unix://* && -S "${endpoint#unix://}" ]]; then
        export RAG_DOCKER_SOCKET="${endpoint#unix://}"
    fi
fi
export RAG_POSTGRES_HOST="" RAG_LANGSMITH_EXPORT=false LANGSMITH_API_KEY=""
compose=(docker compose --project-name jianshu-react --env-file "$env_file" -f compose.yaml)

if [[ "$mode" == "stop" ]]; then
    "${compose[@]}" stop
    exit
fi

if [[ "$mode" == "live" ]] && ! "${compose[@]}" config --environment | awk '
    /^LLM_API_KEY=[[:space:]]*[^[:space:]]/ { configured = 1 }
    END { exit !configured }
'; then
    printf '请先在模型配置文件中填写 LLM_API_KEY。\n' >&2
    exit 1
fi

"${compose[@]}" up -d --build --wait
printf '政策调查 http://127.0.0.1:%s/?workspace=policy-exploration\n演示 API http://127.0.0.1:%s\n' "$RAG_UI_PORT" "$RAG_API_PORT"
