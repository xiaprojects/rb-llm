#!/usr/bin/env bash
# Build llama.cpp (llama-server, llama-bench, llama-cli) at a pinned tag.
#
# Works on a Raspberry Pi 5 (aarch64, Cortex-A76: NEON + dotprod + fp16 via -mcpu=native)
# and on x86_64 dev machines. Binaries are static-linked against ggml/llama and copied to bin/.
#
# Environment:
#   LLAMA_TAG   git tag or commit to build        (default: v1.0.0)
#   LLAMA_REPO  repository URL                     (default: https://github.com/ggml-org/llama.cpp)
#   SRC_DIR     where the source is cloned         (default: third_party/llama.cpp)
#   BIN_DIR     where binaries are installed       (default: bin)
#   JOBS        parallel build jobs                (default: nproc)
#   VULKAN=1    also build the Vulkan backend (Pi 5 VideoCore VII; experimental, needs libvulkan-dev glslc)
set -euo pipefail

LLAMA_TAG="${LLAMA_TAG:-v1.0.0}"
LLAMA_REPO="${LLAMA_REPO:-https://github.com/ggml-org/llama.cpp}"
SRC_DIR="${SRC_DIR:-third_party/llama.cpp}"
BIN_DIR="${BIN_DIR:-bin}"
JOBS="${JOBS:-$(nproc)}"
VULKAN="${VULKAN:-0}"

log() { printf '\033[1;34m[build-llama]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[build-llama]\033[0m %s\n' "$*" >&2; exit 1; }

for tool in git cmake c++ make; do
    command -v "$tool" >/dev/null || die "missing '$tool': run 'make deps' (needs sudo) first"
done

arch="$(uname -m)"
board="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
log "platform: $arch / $board, $JOBS jobs, tag $LLAMA_TAG"
case "$board" in
    *"Raspberry Pi 5"*) ;;
    *"Raspberry Pi"*)   log "warning: not a Pi 5; older Pis are much slower and may lack dotprod" ;;
esac
[ "$arch" = "aarch64" ] || [ "$arch" = "x86_64" ] || log "warning: untested architecture $arch"

# --- source --------------------------------------------------------------
if [ -d "$SRC_DIR/.git" ]; then
    log "updating $SRC_DIR"
    git -C "$SRC_DIR" fetch --depth 1 origin "refs/tags/$LLAMA_TAG:refs/tags/$LLAMA_TAG" 2>/dev/null \
        || git -C "$SRC_DIR" fetch --depth 1 origin "$LLAMA_TAG"
    git -C "$SRC_DIR" checkout -q --force "$LLAMA_TAG" 2>/dev/null || git -C "$SRC_DIR" checkout -q --force FETCH_HEAD
else
    log "cloning $LLAMA_REPO @ $LLAMA_TAG"
    mkdir -p "$(dirname "$SRC_DIR")"
    git clone -q --depth 1 --branch "$LLAMA_TAG" "$LLAMA_REPO" "$SRC_DIR" \
        || { git clone -q "$LLAMA_REPO" "$SRC_DIR" && git -C "$SRC_DIR" checkout -q "$LLAMA_TAG"; }
fi
commit="$(git -C "$SRC_DIR" rev-parse --short HEAD)"

# --- configure -----------------------------------------------------------
cmake_args=(
    -DCMAKE_BUILD_TYPE=Release
    -DBUILD_SHARED_LIBS=OFF        # self-contained binaries, nothing to install in /usr/lib
    -DGGML_NATIVE=ON               # -mcpu=native on the Pi 5, -march=native on x86
    -DLLAMA_CURL=OFF               # models are downloaded by scripts/download-model.sh, not by llama.cpp
    -DLLAMA_OPENSSL=OFF
    -DLLAMA_BUILD_TESTS=OFF
    -DLLAMA_BUILD_EXAMPLES=OFF
    -DLLAMA_BUILD_SERVER=ON
    -DLLAMA_BUILD_UI=OFF           # rb-llm ships its own web UI (web/); llama-server stays API-only
    -DLLAMA_USE_PREBUILT_UI=OFF    # no network fetch of UI assets at configure time (works offline)
)
if [ "$VULKAN" = "1" ]; then
    log "enabling Vulkan backend (experimental)"
    cmake_args+=(-DGGML_VULKAN=ON)
fi

log "configuring"
rm -rf "$SRC_DIR/build/tools/ui"   # stale UI assets from an earlier configure break the build
cmake -S "$SRC_DIR" -B "$SRC_DIR/build" "${cmake_args[@]}" >"$SRC_DIR/build-configure.log" 2>&1 \
    || { tail -30 "$SRC_DIR/build-configure.log"; die "cmake configure failed (log: $SRC_DIR/build-configure.log)"; }

# --- build ---------------------------------------------------------------
targets=(llama-server llama-bench llama-cli)
log "building ${targets[*]} (this takes ~10 min on a Pi 5)"
cmake --build "$SRC_DIR/build" --config Release -j "$JOBS" --target "${targets[@]}" \
    || die "build failed"

# --- install into bin/ ---------------------------------------------------
mkdir -p "$BIN_DIR"
for t in "${targets[@]}"; do
    install -m 0755 "$SRC_DIR/build/bin/$t" "$BIN_DIR/$t"
done
{
    echo "tag=$LLAMA_TAG"
    echo "commit=$commit"
    echo "arch=$arch"
    echo "board=$board"
    echo "vulkan=$VULKAN"
    echo "built=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$BIN_DIR/llama.version"

log "done: $("$BIN_DIR/llama-server" --version 2>&1 | grep -m1 -i version || echo "$LLAMA_TAG ($commit)")"
log "binaries in $BIN_DIR/: ${targets[*]}"
