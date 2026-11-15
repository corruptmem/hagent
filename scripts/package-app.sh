#!/usr/bin/env bash
# Create a self-contained hagent app folder for a local HA app install.
#
# The Supervisor builds a local app with the app folder as the Docker build
# context. The image needs the app sources and the UI sources, which are
# outside hagent/. This script copies hagent/ and the sources into one
# folder.
#
# Usage: scripts/package-app.sh [OUTPUT_DIR]
# The default OUTPUT_DIR is build/app. The result is OUTPUT_DIR/hagent.
# Copy that folder to the "addons" share of HA (/addons/hagent).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-${root}/build/app}"
dest="${out}/hagent"

rm -rf "${dest}"
mkdir -p "${dest}/source/app" "${dest}/source/frontend"

# The app folder: config.yaml, Dockerfile, rootfs/, DOCS.md and the rest.
cp -R "${root}/hagent/." "${dest}/"

# The workspace files that uv needs for a frozen install.
cp "${root}/pyproject.toml" "${root}/uv.lock" "${root}/.python-version" \
  "${dest}/source/"
cp "${root}/app/pyproject.toml" "${dest}/source/app/"
cp -R "${root}/app/src" "${dest}/source/app/src"

# The UI sources, without the local install and build output.
tar -C "${root}/frontend" \
  --exclude=./node_modules --exclude=./dist --exclude='*.tsbuildinfo' \
  -cf - . | tar -C "${dest}/source/frontend" -xf -

# No caches in the build context.
find "${dest}/source" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "${dest}"
