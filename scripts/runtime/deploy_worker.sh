#!/bin/bash
# Thin wrapper around the Python-owned worker lifecycle.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=scripts/lib/find_python.sh
source "$BASE_DIR/scripts/lib/find_python.sh"

exec "$PYTHON" -m netopsbench.platform.runtime.cli deploy "$@"
