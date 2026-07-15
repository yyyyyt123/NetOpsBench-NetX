#!/bin/bash
# Thin standalone teardown wrapper over the Python worker lifecycle.
set -euo pipefail

TOPO_DIR=${1:-clab-topology}
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=scripts/lib/find_python.sh
source "$BASE_DIR/scripts/lib/find_python.sh"

exec "$PYTHON" -m netopsbench.platform.runtime.cli teardown "$TOPO_DIR"
