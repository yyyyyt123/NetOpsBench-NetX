# Pinned Containerlab backend

`containerlab.lock.json` pins the upstream source archive and the complete patch.
The patch is based on Containerlab v0.76.0, commit
`4ecd97b8f149d8ad792df95de360c110f595b442`; its upstream license is retained in
`CONTAINERLAB-LICENSE`. It does not require a modified Docker daemon.

Build with `python -m netopsbench.platform.runtime.build_tools --test` (Go 1.25+,
Git, and a C compiler for race tests). The management controller currently uses
Docker API v1.51; integration was validated with Docker Engine 29.7.2. `--archive` accepts an already downloaded
archive, `--go` selects the compiler, and `--work-dir` retains the patched source.
Executables are local build products under `runtime/_bin/`, excluded from Git
and from the Python distribution. Build after installing the Python package.

The builder enables **both** `links.DirectVethDefault=both` and
`nodes/sonic.DataEndpointDownDefault=on`. The runtime rejects manifests lacking
either setting. Profiling is off by default; `CLAB_PROFILE=1` enables the retained
phase and Docker API instrumentation when invoking the backend directly.

The patch provides:

- Separate container, data-link and post-deploy phases for eligible topologies;
  specialized node types/dependencies retain their original scheduling semantics.
- Both data-link endpoints created directly in their target namespaces, with
  final interface names; SONiC data interfaces remain down until initialization.
- Up to 8 link workers, 32 outstanding SONiC initializations, 4 independent client
  configuration workers, and the configured shared Docker RPC limit.
- A SONiC completion barrier checking services, initialized ports, and startup
  script completion. Normal SDK activation and health checks still follow it.
- At most 16 concurrent per-node container/hosts queries. Hosts collection occurs
  before taking the existing process/file locks; a query failure leaves hosts
  contents intact.
- Single-queue management veth creation and preservation of management metadata
  in Containerlab exports. The Python management controller owns IP reservation,
  per-lab DNS, and management recovery after Docker restarts.

The Python renderer additionally installs `sonic_start_group.py` alongside the
SONiC startup wrapper. This starts dependent service groups with at most four
members per group, without removing services or weakening completion checks.

Management recovery does not recreate Containerlab data links or client agents
after a Docker restart. Recreate the runtime for a complete topology recovery.
Controller state is persisted under `/var/lib/netopsbench/management/<lab>`.
Default SDK teardown selects this backend through the generated
`.netopsbench-managed` marker; older runtimes retain their original cleanup path.

When editing the patch, update its SHA-256 in the lock file, rebuild, run the
focused Go race tests, and validate the ordinary SDK lifecycle. No runtime source
or executable may depend on files under `scenario_results`.
