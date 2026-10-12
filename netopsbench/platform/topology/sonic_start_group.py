"""Bounded Supervisor group start; never restarts failed processes."""

import json
import time
from pathlib import Path

GROUPS = [
    ["coppmgrd", "neighsyncd", "fdbsyncd", "teamsyncd"],
    ["fpmsyncd", "teammgrd", "vrfmgrd", "portmgrd"],
    ["intfmgrd", "vlanmgrd", "zebra"],
    ["mgmtd", "staticd", "buffermgrd", "nbrmgrd"],
    ["vxlanmgrd", "natmgrd", "tunnelmgrd", "fabricmgrd"],
    ["natsyncd"],
]


def start_group(proxy, names, deadline, emit, clock=time.monotonic, sleep=time.sleep, cancelled=lambda: False):
    if len(names) > 4 or len(set(names)) != len(names):
        raise ValueError("Invalid startup group")
    emit(event="group_start", services=names)
    before = {p["name"]: p for p in proxy.getAllProcessInfo()}
    for name in names:
        if cancelled():
            raise InterruptedError("Startup cancelled")
        if clock() >= deadline:
            raise TimeoutError("Service startup deadline")
        if name not in before:
            raise RuntimeError("Missing service " + name)
        if before[name]["statename"] != "STOPPED":
            raise RuntimeError("Unexpected pre-start state " + name + ":" + before[name]["statename"])
        proxy.startProcess(name, False)
        emit(event="submitted", service=name)
    pending = set(names)
    while pending:
        if cancelled():
            raise InterruptedError("Startup cancelled")
        if clock() >= deadline:
            raise TimeoutError("Service startup deadline")
        states = {p["name"]: p for p in proxy.getAllProcessInfo()}
        for name in list(pending):
            p = states.get(name, {})
            if p.get("statename") == "RUNNING":
                pending.remove(name)
                emit(event="running", service=name, pid=p.get("pid"))
            elif p.get("statename") != "STARTING":
                raise RuntimeError("Service startup failed " + name + ":" + str(p.get("statename")))
        # Also catch an earlier service dying while a later member starts.
        if any(states.get(n, {}).get("statename") != "RUNNING" for n in set(names) - pending):
            raise RuntimeError("Started group member exited")
        if pending:
            sleep(min(0.5, max(0, deadline - clock())))
    emit(event="group_end", services=names)


def _main():
    import socket

    from supervisor.options import ClientOptions

    socket.setdefaulttimeout(2)
    options = ClientOptions()
    options.realize(args=[])
    path = Path("/tmp/netopsbench-service-start.jsonl")

    def emit(**row):
        with path.open("a") as f:
            f.write(json.dumps(dict(utc=time.time(), monotonic=time.monotonic(), **row)) + "\n")

    deadline = time.monotonic() + 115
    try:
        with options.getServerProxy() as proxy:
            for names in GROUPS:
                start_group(proxy.supervisor, names, deadline, emit)
            processes = {p["name"]: p for p in proxy.supervisor.getAllProcessInfo()}
            failed = [n for group in GROUPS for n in group if processes[n]["statename"] != "RUNNING"]
            if failed:
                raise RuntimeError("Service no longer running: " + ",".join(failed))
    except BaseException as e:
        emit(event="failed", error_type=type(e).__name__, error=str(e))
        raise


if __name__ == "__main__":
    _main()
