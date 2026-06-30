#!/usr/bin/env bash
#
# pi-memory setup
#
# Idempotent local setup for the pi-memory extension:
#   1. symlinks the extension into .pi/extensions/ so pi auto-discovers it
#   2. creates pi-memory/.env from .env.example if it is missing
#   3. builds the sidecar uv venv + installs deps
#
# Safe to re-run. Run from anywhere; paths are resolved relative to this file.

set -euo pipefail

PI_MEMORY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${PI_MEMORY_DIR}/.." && pwd)"

echo "==> pi-memory setup (repo: ${REPO_ROOT})"

# 1. Auto-discovery symlink: .pi/extensions/pi-memory.ts -> pi-memory/extension/index.ts
EXT_DIR="${REPO_ROOT}/.pi/extensions"
LINK="${EXT_DIR}/pi-memory.ts"
TARGET_REL="../../pi-memory/extension/index.ts"

mkdir -p "${EXT_DIR}"
ln -sf "${TARGET_REL}" "${LINK}"
echo "    [ok] symlinked ${LINK} -> ${TARGET_REL}"

# 2. .env from example (never clobber an existing .env)
ENV_FILE="${PI_MEMORY_DIR}/.env"
ENV_EXAMPLE="${PI_MEMORY_DIR}/.env.example"
if [[ -f "${ENV_FILE}" ]]; then
  echo "    [skip] ${ENV_FILE} already exists"
elif [[ -f "${ENV_EXAMPLE}" ]]; then
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  echo "    [ok] created ${ENV_FILE} from .env.example — edit it (instance, profile, user id)"
else
  echo "    [warn] ${ENV_EXAMPLE} missing; skipping .env creation"
fi

# 3. Sidecar venv + deps
SIDECAR_DIR="${PI_MEMORY_DIR}/sidecar"
if command -v uv >/dev/null 2>&1; then
  echo "==> building sidecar venv (${SIDECAR_DIR})"
  ( cd "${SIDECAR_DIR}" && uv venv --python 3.12 && uv sync )
  echo "    [ok] sidecar venv ready"
else
  echo "    [warn] 'uv' not found on PATH; skipping sidecar venv."
  echo "           Install uv (https://docs.astral.sh/uv/) then run:"
  echo "             cd ${SIDECAR_DIR} && uv venv --python 3.12 && uv sync"
fi

cat <<'EOF'

==> Done. Next:
  - Ensure you are logged in:   databricks auth login --profile <profile>
  - Launch pi from the repo root (trust the project when prompted):
      pi                 # extension auto-loads; memory OFF by default
      pi --memory        # extension auto-loads; memory ON from launch
  - In session:  /memory on | off | status
EOF
