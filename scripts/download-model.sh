#!/usr/bin/env bash
# Download a model from models/registry.tsv and verify its SHA-256.
#   scripts/download-model.sh <id>        download (resumes partial downloads)
#   scripts/download-model.sh --list      list registered models
# Environment: MODELS_DIR (default: models), HF_ENDPOINT (default: https://huggingface.co)
set -euo pipefail

REGISTRY="${REGISTRY:-models/registry.tsv}"
MODELS_DIR="${MODELS_DIR:-models}"
HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"

log() { printf '\033[1;34m[model]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[model]\033[0m %s\n' "$*" >&2; exit 1; }

entries() { grep -v '^#' "$REGISTRY" | grep -v '^[[:space:]]*$'; }

if [ "${1:-}" = "--list" ] || [ -z "${1:-}" ]; then
    printf '%-18s %-9s %-20s %s\n' ID SIZE LICENSE NOTES
    entries | while IFS=$'\t' read -r id repo file sha size lic notes; do
        mark=" "; [ -f "$MODELS_DIR/$file" ] && mark="*"
        printf '%-18s %-9s %-20s %s\n' "$mark$id" "$((size / 1000000))MB" "$lic" "$notes"
    done
    echo "(* = downloaded)"
    [ -n "${1:-}" ] || exit 1
    exit 0
fi

id="$1"
line="$(entries | awk -F'\t' -v id="$id" '$1 == id')"
[ -n "$line" ] || die "unknown model '$id' (see: $0 --list)"
IFS=$'\t' read -r _ repo file sha size lic notes <<<"$line"

mkdir -p "$MODELS_DIR"
dest="$MODELS_DIR/$file"

verify() { echo "$sha  $1" | sha256sum -c --status; }

if [ -f "$dest" ]; then
    log "$file present, verifying checksum"
    if verify "$dest"; then
        log "ok"
    else
        die "checksum mismatch for existing $dest: delete it and re-run"
    fi
else
    free_kb="$(df -Pk "$MODELS_DIR" | awk 'NR==2 {print $4}')"
    [ "$free_kb" -gt $((size / 1024 + 102400)) ] || die "not enough disk space for $file ($((size / 1000000)) MB)"
    url="$HF_ENDPOINT/$repo/resolve/main/$file"
    log "downloading $file ($((size / 1000000)) MB, license $lic)"
    log "from $url"
    curl -fL --retry 5 --retry-delay 3 -C - --progress-bar -o "$dest.part" "$url"
    log "verifying checksum"
    verify "$dest.part" || { rm -f "$dest.part"; die "checksum mismatch: download corrupted or upstream file changed"; }
    mv "$dest.part" "$dest"
fi

ln -sfn "$file" "$MODELS_DIR/current.gguf"
echo "$id" >"$MODELS_DIR/current.id"
log "active model: $id -> $MODELS_DIR/current.gguf"
log "license: $lic. You are responsible for complying with it."
