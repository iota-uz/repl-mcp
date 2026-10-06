#!/bin/sh
# Launch the published native wheel; retain the caller's working directory.
set -eu
version=3.1.0
os=$(uname -s)
arch=$(uname -m)
case "$os:$arch" in
  Darwin:arm64) platform=macosx_11_0_arm64 ;;
  Darwin:x86_64) platform=macosx_11_0_x86_64 ;;
  Linux:x86_64) platform=manylinux_2_28_x86_64 ;;
  *)
    printf 'repl-mcp: unsupported host %s/%s; use macOS arm64/x86_64 or Linux x86_64 (glibc 2.28+).\n' "$os" "$arch" >&2
    exit 1
    ;;
esac
if ! command -v uvx >/dev/null 2>&1; then
  printf 'repl-mcp: uvx is required; install uv from https://docs.astral.sh/uv/getting-started/installation/ or register an installed repl-mcp executable.\n' >&2
  exit 1
fi
wheel="https://github.com/iota-uz/repl-mcp/releases/download/v${version}/repl_mcp-${version}-py3-none-${platform}.whl"
exec uvx --from "$wheel" repl-mcp "$@"
