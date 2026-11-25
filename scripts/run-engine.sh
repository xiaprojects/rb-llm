#!/usr/bin/env bash
# Start llama-server for rb-llm with the active model (models/current.gguf).
# Environment (all optional):
#   MODEL_FILE   GGUF to load                 (default: models/current.gguf)
#   LLAMA_HOST   bind address                 (default: 127.0.0.1 — only rb-llm talks to it)
#   LLAMA_PORT   port                         (default: 8080)
#   THREADS      CPU threads                  (default: physical cores; use 3 when co-located with rbvoice)
#   SLOTS        parallel slots = tasks       (default: 3: command, atc, ask — keeps each prompt cached)
#   CTX_PER_SLOT context tokens per slot      (default: 4096)
#   LOAD_MODE    auto | mmap+mlock ...        (default: auto; systemd unit uses mmap+mlock)
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL_FILE="${MODEL_FILE:-models/current.gguf}"
LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
# physical cores: hyper-threads slow llama.cpp down (Pi 5: 4 cores, no SMT)
phys="$(lscpu -p=Core,Socket 2>/dev/null | grep -v '^#' | sort -u | wc -l)"
THREADS="${THREADS:-${phys:-$(nproc)}}"
[ "$THREADS" -ge 1 ] 2>/dev/null || THREADS="$(nproc)"
SLOTS="${SLOTS:-3}"
CTX_PER_SLOT="${CTX_PER_SLOT:-4096}"
LOAD_MODE="${LOAD_MODE:-auto}"
BIN="${BIN:-bin/llama-server}"

[ -x "$BIN" ] || { echo "llama-server not built: run 'make llama'" >&2; exit 1; }
[ -e "$MODEL_FILE" ] || { echo "no model at $MODEL_FILE: run 'make model'" >&2; exit 1; }
alias_name="$(cat models/current.id 2>/dev/null || basename "$MODEL_FILE" .gguf)"

exec "$BIN" \
    --model "$MODEL_FILE" --alias "$alias_name" \
    --host "$LLAMA_HOST" --port "$LLAMA_PORT" \
    --threads "$THREADS" \
    --parallel "$SLOTS" --ctx-size "$((SLOTS * CTX_PER_SLOT))" \
    --cache-reuse 256 \
    --reasoning off \
    --jinja \
    --load-mode "$LOAD_MODE" \
    --no-webui \
    --metrics
