#!/bin/bash
# NetOpsBench One-Command Deployment Script
# Deploys complete system: topology + observability stack

set -e

TOPO_SCALE=${1:-xs}
TOPO_DIR=${2:-lab-topology}
LAB_NAME=${NETOPSBENCH_LAB_NAME:-dcn}
MGMT_SUBNET=${NETOPSBENCH_MGMT_SUBNET:-}
MGMT_NETWORK=${NETOPSBENCH_MGMT_NETWORK:-}

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
else
    # Non-interactive sudo: fail fast instead of prompting for password.
    SUDO=("sudo" "-n")
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$BASE_DIR"

# Locate project Python interpreter (prefers repo venv, falls back to system python3).
# Override via NETOPSBENCH_PYTHON env var if needed.
# shellcheck source=scripts/lib/find_python.sh
source "$BASE_DIR/scripts/lib/find_python.sh"

echo "=== NetOpsBench Deployment Start ==="
echo "Topology scale: $TOPO_SCALE"
echo "Topology directory: $TOPO_DIR"
echo "Lab name: $LAB_NAME"
if [ -n "$MGMT_SUBNET" ]; then
    echo "Management subnet override: $MGMT_SUBNET"
fi
echo ""

# Resolve topology directory (always-generate mode).
# Rule:
#   - If a base dir is provided, generated topology goes to:
#       <base>/generated_topology_<scale>
#   - If an explicit generated dir is provided, use it as-is.
ACTUAL_TOPO_DIR="$TOPO_DIR"
if [ "$TOPO_DIR" = "generated_topology" ]; then
    ACTUAL_TOPO_DIR="lab-topology/generated_topology_${TOPO_SCALE}"
elif echo "$TOPO_DIR" | grep -Eq '(^|/)generated_topology_(xs|small|medium|large|xlarge)$'; then
    ACTUAL_TOPO_DIR="$TOPO_DIR"
else
    ACTUAL_TOPO_DIR="$TOPO_DIR/generated_topology_${TOPO_SCALE}"
fi

mkdir -p "$ACTUAL_TOPO_DIR"

TOPOLOGY_FILE="$ACTUAL_TOPO_DIR/${LAB_NAME}.clab.yaml"
TOPOLOGY_ID=${NETOPSBENCH_TOPOLOGY_ID:-$(basename "$ACTUAL_TOPO_DIR")}
echo "Resolved topology directory: $ACTUAL_TOPO_DIR"
echo "Topology ID: $TOPOLOGY_ID"
echo ""

# [1/7] Generate topology for requested scale
echo "[1/7] Generating topology (scale=$TOPO_SCALE) into: $ACTUAL_TOPO_DIR"
$PYTHON -c "
from netopsbench.platform.topology.generator import generate_topology
result = generate_topology(
    '$TOPO_SCALE',
    '$ACTUAL_TOPO_DIR',
    name='$LAB_NAME',
    mgmt_subnet='${MGMT_SUBNET}',
    mgmt_network='${MGMT_NETWORK}',
)
print(f'Generated topology: {result[\"yaml_file\"]}')
print(f'Generated metadata: {result[\"metadata_file\"]}')
"
echo ""

# [2/7] Deploy Containerlab topology
echo "[2/7] Deploying Containerlab topology..."
if [ ! -f "$TOPOLOGY_FILE" ]; then
    echo "ERROR: Topology file not found: $TOPOLOGY_FILE"
    exit 1
fi

CLAB_DEPLOY_ARGS=(deploy -t "$TOPOLOGY_FILE" --reconfigure)
if [ -n "${NETOPSBENCH_CONTAINERLAB_MAX_WORKERS:-}" ]; then
    CLAB_DEPLOY_ARGS+=(--max-workers "$NETOPSBENCH_CONTAINERLAB_MAX_WORKERS")
fi
if [ -n "${NETOPSBENCH_CONTAINERLAB_TIMEOUT:-}" ]; then
    CLAB_DEPLOY_ARGS+=(--timeout "$NETOPSBENCH_CONTAINERLAB_TIMEOUT")
fi
"${SUDO[@]}" containerlab "${CLAB_DEPLOY_ARGS[@]}"
echo ""

# [2.5/7] Apply SONiC configurations using fast parallel application
echo "[2.5/7] Applying device configurations (SONiC)..."
APPLY_CONFIG_PARALLEL=${NETOPSBENCH_APPLY_CONFIG_PARALLEL:-32}
$PYTHON -m netopsbench.platform.runtime.apply_configs "$ACTUAL_TOPO_DIR" "$APPLY_CONFIG_PARALLEL" "$LAB_NAME"
echo ""

# [3/7] Generate or verify topology metadata
echo "[3/7] Checking topology metadata..."
METADATA_FILE="$ACTUAL_TOPO_DIR/topology.json"

if [ ! -f "$METADATA_FILE" ]; then
    echo "  Topology metadata not found, generating from YAML..."
    $PYTHON -c "
from netopsbench.platform.topology.metadata_generator import generate_metadata_file
generate_metadata_file('$TOPOLOGY_FILE', '$METADATA_FILE')
"
    if [ ! -f "$METADATA_FILE" ]; then
        echo "ERROR: Failed to generate topology metadata"
        exit 1
    fi
else
    echo "  Using existing metadata: $METADATA_FILE"
fi
echo ""

# [5/7] Start observability stack
echo "[5/7] Starting observability stack..."
cd observability
"${SUDO[@]}" docker compose up -d
cd "$BASE_DIR"
echo ""

# Attach shared InfluxDB to the lab management network so lab containers can
# resolve and reach it via the stable hostname "influxdb".
MGMT_NETWORK_NAME=$($PYTHON - <<PY
import json
with open("$METADATA_FILE") as f:
    topo = json.load(f)
print((topo.get("management", {}) or {}).get("network", "").strip())
PY
)
if [ -n "$MGMT_NETWORK_NAME" ]; then
    echo "[5.5/7] Attaching InfluxDB to lab management network..."
    "${SUDO[@]}" docker network connect "$MGMT_NETWORK_NAME" influxdb --alias influxdb >/dev/null 2>&1 || true
    echo "  Attached influxdb to $MGMT_NETWORK_NAME"
    echo ""
fi

# [6/7] Start per-lab Telegraf sidecar
echo "[6/7] Starting worker Telegraf..."
BGP_COLLECTOR_PID_FILE="$ACTUAL_TOPO_DIR/bgp_collector.pid"
BGP_COLLECTOR_LOG_FILE="$ACTUAL_TOPO_DIR/bgp_collector.log"
BGP_COLLECTOR_OUTPUT_FILE="$ACTUAL_TOPO_DIR/bgp_neighbors.lp"
BGP_PID=$(cat "$BGP_COLLECTOR_PID_FILE" 2>/dev/null || true)
if [ -n "$BGP_PID" ] && kill -0 "$BGP_PID" >/dev/null 2>&1; then
    kill "$BGP_PID" >/dev/null 2>&1 || true
fi
rm -f "$BGP_COLLECTOR_PID_FILE"
NETOPSBENCH_TOPOLOGY_ID="$TOPOLOGY_ID" \
NETOPSBENCH_TELEGRAF_INFLUXDB_URL="${NETOPSBENCH_TELEGRAF_INFLUXDB_URL:-http://influxdb:8086}" \
    bash scripts/observability/start_worker_telegraf.sh "$ACTUAL_TOPO_DIR" "telegraf-${LAB_NAME}"

if command -v setsid >/dev/null 2>&1; then
    BGP_COLLECTOR_DETACH=(nohup setsid)
else
    BGP_COLLECTOR_DETACH=(nohup)
fi
NETOPSBENCH_TOPOLOGY_ID="$TOPOLOGY_ID" \
    "${BGP_COLLECTOR_DETACH[@]}" $PYTHON scripts/runtime/run_bgp_collector.py \
    "$METADATA_FILE" \
    --output "$BGP_COLLECTOR_OUTPUT_FILE" \
    --interval "${NETOPSBENCH_BGP_POLL_INTERVAL_SECONDS:-10}" \
    --parallelism "${NETOPSBENCH_BGP_COLLECTOR_PARALLELISM:-16}" \
    >> "$BGP_COLLECTOR_LOG_FILE" 2>&1 &
echo $! > "$BGP_COLLECTOR_PID_FILE"
sleep 5
echo ""

# [7/7] Deploy Pingmesh agents
echo "[7/7] Deploying Pingmesh agents..."
NETOPSBENCH_TOPOLOGY_ID="$TOPOLOGY_ID" \
    $PYTHON -m netopsbench.platform.pingmesh.deploy "$ACTUAL_TOPO_DIR/configs/pingmesh/pinglist.json" "$ACTUAL_TOPO_DIR"
sleep 5
echo ""

# Verify deployment
echo "=== Deployment Verification ==="
echo ""
echo "Network devices:"
"${SUDO[@]}" docker ps --filter "name=clab-${LAB_NAME}" --format "table {{.Names}}\t{{.Status}}" | head -10
echo ""
echo "Observability stack:"
"${SUDO[@]}" docker ps --filter "name=influxdb" --format "table {{.Names}}\t{{.Status}}"
"${SUDO[@]}" docker ps --filter "name=grafana" --format "table {{.Names}}\t{{.Status}}"
"${SUDO[@]}" docker ps --filter "name=telegraf" --format "table {{.Names}}\t{{.Status}}"
echo ""

echo "=== Deployment Complete ==="
echo ""
echo "Access points:"
echo "  Grafana:  http://localhost:3000"
echo "  InfluxDB: http://localhost:8086"
echo ""
echo "Next steps:"
echo "  1. Wait ~30s for Pingmesh metrics to start flowing"
echo "  2. Open Grafana to view dashboards and Pingmesh monitoring"
echo "  3. Export NETOPSBENCH_TOPOLOGY_DIR to this directory so AgentToolkit / SDK resolve topology.json"
echo "  4. Run scenarios via the Python SDK (see README + examples/integration/sdk_e2e_runtime_*.py)"
echo "  5. CLI (optional): netopsbench scenario list / netopsbench scenario validate <file>"
echo ""
