#!/bin/bash
# Thin standalone deployment wrapper over the Python worker lifecycle.
set -euo pipefail

TOPO_SCALE=${1:-xs}
TOPO_DIR=${2:-lab-topology}
LAB_NAME=${NETOPSBENCH_LAB_NAME:-dcn}
MGMT_SUBNET=${NETOPSBENCH_MGMT_SUBNET:-}
MGMT_NETWORK=${NETOPSBENCH_MGMT_NETWORK:-}
INFLUXDB_BUCKET=${NETOPSBENCH_INFLUXDB_BUCKET:-netopsbench}

if [ "$TOPO_DIR" = "generated_topology" ]; then
    ACTUAL_TOPO_DIR="lab-topology/generated_topology_${TOPO_SCALE}"
elif [[ "$TOPO_DIR" =~ (^|/)generated_topology_[^/]+$ ]]; then
    ACTUAL_TOPO_DIR="$TOPO_DIR"
else
    ACTUAL_TOPO_DIR="$TOPO_DIR/generated_topology_${TOPO_SCALE}"
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=scripts/lib/find_python.sh
source "$BASE_DIR/scripts/lib/find_python.sh"

exec "$PYTHON" -m netopsbench.platform.runtime.cli deploy \
    "$TOPO_SCALE" "$ACTUAL_TOPO_DIR" "$LAB_NAME" "$MGMT_SUBNET" "$INFLUXDB_BUCKET" \
    --mgmt-network "$MGMT_NETWORK"
