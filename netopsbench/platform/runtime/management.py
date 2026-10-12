"""Management veth, DNS and restart lifecycle for the pinned Containerlab backend."""

import concurrent.futures
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import dns.message
import dns.query
import dns.rcode
import dns.rdatatype
import dns.resolver
import dns.rrset
import yaml

from netopsbench.platform.runtime.toolchain import runtime_tools

STATE_ROOT = Path("/var/lib/netopsbench/management")
LABEL = "netopsbench.management.v1"
OWNER = "netopsbench.management.runtime"


def write(p, x):
    p = Path(p)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(x, indent=2))
    tmp.replace(p)


def log(p, **x):
    with open(p, "a") as f:
        f.write(json.dumps(dict(utc=time.time(), monotonic=time.monotonic(), **x)) + "\n")


class Conn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=180)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def api(method, path, data=None):
    c = Conn()
    try:
        c.request(
            method,
            "/v1.51" + path,
            body=None if data is None else json.dumps(data),
            headers={"Content-Type": "application/json"},
        )
        r = c.getresponse()
        b = r.read()
        if r.status == 404:
            return None
        if r.status >= 300:
            raise RuntimeError(f"Docker {method} {path.split('?')[0]} status {r.status}: {b[:200]!r}")
        return json.loads(b) if b else None
    finally:
        c.close()


def birth(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def helper(m, pid=None, mode="ensure"):
    req = dict(
        mode=mode,
        host=m["Host"],
        bridge=m["Bridge"],
        cidr=f"{m['IPv4addr']}/{m['IPv4pLen']}",
        gateway=m["IPv4Gw"],
        alias=m["Alias"],
    )
    if pid:
        req.update(pid=pid, birth=birth(pid))
    p = subprocess.run(
        [str(runtime_tools() / "mgmt-link")], input=json.dumps(req), text=True, capture_output=True, timeout=30
    )
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    return json.loads(p.stdout)


def container_list(lab):
    from urllib.parse import urlencode

    return api(
        "GET",
        "/containers/json?" + urlencode({"all": "true", "filters": json.dumps({"label": ["containerlab=" + lab]})}),
    )


def state_dir(lab):
    if (
        not lab
        or len(lab) > 100
        or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in lab)
    ):
        raise ValueError("invalid lab name")
    return STATE_ROOT / lab


def prepare(path):
    p = Path(path).resolve()
    top = yaml.safe_load(p.read_text())
    lab = top["name"]
    d = state_dir(lab)
    d.mkdir(parents=True, exist_ok=True)
    if d.is_symlink():
        raise RuntimeError("Management state must not be a symlink")
    if (d / "manifest.json").exists():
        if not (d / "cleaned.json").exists() or container_list(lab):
            raise RuntimeError("Existing management state; destroy explicitly before deploying again")
        import shutil

        shutil.rmtree(d)
        d.mkdir(parents=True)
    mg = top["mgmt"]
    net = ipaddress.ip_network(mg["ipv4-subnet"])
    gw = str(net.network_address + 1)
    if net.version != 4 or mg.get("ipv6-subnet"):
        raise ValueError("Generated runtimes require explicit IPv4 management")
    nodes = top["topology"]["nodes"]
    ips = [n["mgmt-ipv4"] for n in nodes.values()]
    if len(set(ips)) != len(ips) or any(
        ipaddress.ip_address(ip) not in net or ip in [gw, str(net.network_address), str(net.broadcast_address)]
        for ip in ips
    ):
        raise ValueError("Invalid or duplicate management addresses")
    network = mg["network"]
    bridge = "br-nbm" + hashlib.sha256(lab.encode()).hexdigest()[:8]
    if api("GET", "/networks/" + network) is not None:
        raise RuntimeError("Refusing to adopt existing network")
    (d / "original.clab.yaml").write_text(p.read_text())
    meta = {}
    records = {}
    for name, node in nodes.items():
        full = f"clab-{lab}-{name}"
        ip = node["mgmt-ipv4"]
        m = dict(
            IPv4addr=ip,
            IPv4pLen=net.prefixlen,
            IPv4Gw=gw,
            Network=network,
            Bridge=bridge,
            Host="nbm" + hashlib.sha256(full.encode()).hexdigest()[:12],
            Alias=f"netopsbench-mgmt:{lab}:{name}",
        )
        meta[full] = m
        node.setdefault("labels", {})[LABEL] = json.dumps(m, separators=(",", ":"))
        for alias in [name, full, *node.get("aliases", [])]:
            for a in [alias, alias + "." + lab, alias + "." + lab + ".lab"]:
                if a.lower() in records and records[a.lower()] != ip:
                    raise ValueError("DNS alias collision")
                records[a.lower()] = ip
        node["dns"] = {"servers": [gw]}
    manifest = dict(
        lab=lab,
        network=network,
        bridge=bridge,
        gateway=gw,
        nodes=meta,
        records=records,
        created=time.time(),
        topology=str(p),
    )
    write(d / "manifest.json", manifest)
    try:
        api(
            "POST",
            "/networks/create",
            {
                "Name": network,
                "Driver": "bridge",
                "EnableIPv6": False,
                "IPAM": {
                    "Driver": "default",
                    "Config": [
                        {
                            "Subnet": str(net),
                            "Gateway": gw,
                            "AuxiliaryAddresses": {name: n["mgmt-ipv4"] for name, n in nodes.items()},
                        }
                    ],
                },
                "Labels": {"containerlab": "", OWNER: lab},
                "Options": {
                    "com.docker.network.bridge.name": bridge,
                    "com.docker.network.bridge.gateway_mode_ipv4": "nat-unprotected",
                    "com.docker.network.bridge.gateway_mode_ipv6": "nat-unprotected",
                    "com.docker.network.driver.mtu": "1500",
                },
            },
        )
        p.write_text(yaml.safe_dump(top, sort_keys=False))
        start_daemon(d)
    except BaseException:
        stop_daemon(d)
        cleanup_network(d)
        raise
    return d


def start_daemon(d):
    (d / "stop").unlink(missing_ok=True)
    (d / "ready").unlink(missing_ok=True)
    with open(d / "daemon.log", "a") as f:
        proc = subprocess.Popen(
            [sys.executable, "-m", "netopsbench.platform.runtime.management", "daemon", str(d)],
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=f,
            start_new_session=True,
        )
    write(d / "daemon.json", dict(pid=proc.pid, birth=birth(proc.pid)))
    for _ in range(100):
        if (d / "ready").exists():
            return
        if proc.poll() is not None:
            raise RuntimeError("Management controller startup failed; see daemon.log")
        time.sleep(0.1)
    raise TimeoutError("Management controller startup timeout")


def stop_daemon(d):
    if not d.exists():
        return
    (d / "stop").touch()
    if not (d / "daemon.json").exists():
        return
    info = json.loads((d / "daemon.json").read_text())
    for _ in range(50):
        try:
            if birth(info["pid"]) != info["birth"]:
                return
            if Path(f"/proc/{info['pid']}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return
        except FileNotFoundError:
            return
        time.sleep(0.1)
    if birth(info["pid"]) == info["birth"]:
        os.kill(info["pid"], signal.SIGTERM)


def cleanup_network(d):
    m = json.loads((d / "manifest.json").read_text())
    n = api("GET", "/networks/" + m["network"])
    if n:
        if n.get("Labels", {}).get(OWNER) != m["lab"]:
            raise RuntimeError("Network ownership mismatch")
        if n.get("Containers"):
            raise RuntimeError("Network still has attached Docker endpoints")
    if container_list(m["lab"]):
        raise RuntimeError("Own containers remain after destruction")
    errors = []
    for x in m["nodes"].values():
        try:
            helper(x, mode="delete")
        except Exception as e:
            errors.append(str(e))
    if errors:
        raise RuntimeError(str(errors))
    if n:
        api("DELETE", "/networks/" + m["network"])
    # Destroy-by-name after an interrupted SDK registration may omit these
    # paths. Remove only exact entries belonging to this manifest.
    for name, meta in m["nodes"].items():
        nslink = Path("/run/netns") / name
        if nslink.is_symlink():
            nslink.unlink()
        lock = Path("/run/netopsbench-mgmt") / (meta["Host"] + ".lock")
        lock.unlink(missing_ok=True)
    ssh = Path("/etc/ssh/ssh_config.d") / ("clab-" + m["lab"] + ".conf")
    if ssh.exists():
        if m["lab"] not in ssh.read_text():
            raise RuntimeError("SSH config ownership mismatch")
        ssh.unlink()
    write(d / "cleaned.json", dict(utc=time.time(), success=True))


class BoundedDNSRequests:
    """Keep upstream DNS sockets bounded during synchronized client lookups."""

    max_requests = 32

    def __init__(self, *args, **kwargs):
        self.request_slots = threading.BoundedSemaphore(self.max_requests)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class UDP(BoundedDNSRequests, socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


class TCP(BoundedDNSRequests, socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class UHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        try:
            sock.sendto(self.server.answer(data), self.client_address)
        except Exception:
            pass


class THandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)

        def read(n):
            b = b""
            while len(b) < n:
                x = self.request.recv(n - len(b))
                if not x:
                    raise EOFError
                b += x
            return b

        try:
            n = int.from_bytes(read(2), "big")
            out = self.server.answer(read(n), tcp=True)
            self.request.sendall(len(out).to_bytes(2, "big") + out)
        except Exception:
            pass


def dns_answer(wire, records, upstreams, tcp=False):
    q = dns.message.from_wire(wire)
    r = dns.message.make_response(q)
    if len(q.question) != 1:
        r.set_rcode(dns.rcode.FORMERR)
        return r.to_wire()
    question = q.question[0]
    name = question.name.to_text().rstrip(".").lower()
    if name in records:
        if question.rdtype in (dns.rdatatype.A, dns.rdatatype.ANY):
            r.answer.append(dns.rrset.from_text(question.name, 5, "IN", "A", records[name]))
        return r.to_wire()
    if name.endswith(".in-addr.arpa") and question.rdtype == dns.rdatatype.PTR:
        ip = ".".join(reversed(name.removesuffix(".in-addr.arpa").split(".")))
        names = sorted(k for k, v in records.items() if v == ip and "." not in k)
        if names:
            r.answer.append(dns.rrset.from_text(question.name, 5, "IN", "PTR", names[0] + "."))
            return r.to_wire()
    for ns in upstreams:
        try:
            a = dns.query.tcp(q, ns, timeout=2) if tcp else dns.query.udp(q, ns, timeout=2)
            if a.flags & dns.flags.TC:
                a = dns.query.tcp(q, ns, timeout=2)
            return a.to_wire()
        except Exception:
            continue
    r.set_rcode(dns.rcode.SERVFAIL)
    return r.to_wire()


class DNSRegistry:
    """Merge static topology names with Docker endpoints on this exact lab bridge."""

    def __init__(self, manifest, request=api, clock=time.monotonic):
        self.manifest = manifest
        self.request = request
        self.clock = clock
        self.lock = threading.Lock()
        self.updated = -100.0
        self.records = dict(manifest["records"])
        self.aliases = {}

    def snapshot(self, force=False):
        if not force and self.clock() - self.updated < 1:
            return self.records
        with self.lock:
            if not force and self.clock() - self.updated < 1:
                return self.records
            network = self.request("GET", "/networks/" + self.manifest["network"])
            if network is None:
                return self.records
            records = {}
            cache = {}
            for cid, endpoint in network.get("Containers", {}).items():
                ip = endpoint.get("IPv4Address", "").split("/")[0]
                if not ip:
                    continue
                key = (cid, endpoint.get("EndpointID"))
                aliases = self.aliases.get(key)
                if aliases is None:
                    c = self.request("GET", "/containers/" + cid + "/json")
                    cfg = (c or {}).get("NetworkSettings", {}).get("Networks", {}).get(self.manifest["network"], {})
                    aliases = [endpoint["Name"], *(cfg.get("Aliases") or []), *(cfg.get("DNSNames") or [])]
                cache[key] = aliases
                for alias in aliases:
                    if alias:
                        records[alias.lower().rstrip(".")] = ip
            records.update(self.manifest["records"])
            self.aliases = cache
            self.records = records
            self.updated = self.clock()
            return self.records


def daemon(d):
    m = json.loads((d / "manifest.json").read_text())
    pending = queue.Queue()
    known = set()
    servers = []
    # One controller per lab, including restart of the controller itself.
    lock = open(d / "controller.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    upstreams = list(dns.resolver.Resolver().nameservers)
    registry = DNSRegistry(m)
    for cls, handler in [(UDP, UHandler), (TCP, THandler)]:
        server = cls((m["gateway"], 53), handler)
        server.answer = lambda wire, tcp=False: dns_answer(wire, registry.snapshot(), upstreams, tcp)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    events = subprocess.Popen(
        [
            "docker",
            "events",
            "--filter",
            "label=containerlab=" + m["lab"],
            "--filter",
            "type=container",
            "--format",
            "{{json .}}",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )

    def consume():
        for line in events.stdout:
            try:
                e = json.loads(line)
                if e.get("Action") in ("start", "restart"):
                    pending.put(e["Actor"]["ID"])
            except Exception:
                pass

    threading.Thread(target=consume, daemon=True).start()

    def reconcile(cid):
        try:
            c = api("GET", "/containers/" + cid + "/json")
            if not c or not c["State"]["Running"]:
                return
            name = c["Name"].lstrip("/")
            expected = m["nodes"].get(name)
            if expected is None or json.loads(c["Config"]["Labels"].get(LABEL, "null")) != expected:
                raise ValueError("Container metadata ownership mismatch")
            result = helper(expected, c["State"]["Pid"])
            nslink = Path("/run/netns") / name
            target = f"/proc/{c['State']['Pid']}/ns/net"
            if nslink.is_symlink() and os.readlink(nslink) != target:
                temp = nslink.with_name(name + ".mgmt-tmp")
                temp.unlink(missing_ok=True)
                temp.symlink_to(target)
                temp.replace(nslink)
            log(d / "reconcile.jsonl", container=name, pid=c["State"]["Pid"], status="ok", result=result)
        except Exception as e:
            log(d / "reconcile.jsonl", container=cid[:12], status="error", error=str(e))
            pending.put(cid)

    (d / "ready").touch()
    last = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            while not (d / "stop").exists():
                if events.poll() is not None:
                    raise RuntimeError("Docker event stream ended")
                ids = set()
                if time.monotonic() - last > 5:
                    rows = container_list(m["lab"])
                    current = {c["Id"] for c in rows if c["State"] == "running"}
                    ids |= current - known
                    known = current
                    last = time.monotonic()
                    if api("GET", "/networks/" + m["network"]) is None:
                        break
                for _ in range(pending.qsize()):
                    ids.add(pending.get_nowait())
                list(pool.map(reconcile, ids))
                time.sleep(0.25)
    finally:
        events.terminate()
        events.wait(timeout=10)
        for server in servers:
            server.shutdown()
            server.server_close()
        (d / "ready").unlink(missing_ok=True)
        log(d / "reconcile.jsonl", status="controller_stopped")


def cli(args):
    if not args:
        raise ValueError("missing command")
    command = args[0]
    d = None
    lab = ""
    if command == "deploy":
        if "-t" not in args:
            raise ValueError("explicit topology required")
        d = prepare(args[args.index("-t") + 1])
        lab = d.name
        # A freshly created network is reserved and owned by this run; no previous topology exists.
        args = [a for a in args if a != "--reconfigure"]
    elif command == "destroy":
        if "-t" in args:
            lab = yaml.safe_load(Path(args[args.index("-t") + 1]).read_text())["name"]
        elif "--name" in args:
            lab = args[args.index("--name") + 1]
        else:
            raise ValueError("explicit lab required")
        d = state_dir(lab)
        stop_daemon(d)
    binary = runtime_tools() / "containerlab"
    result = subprocess.run([str(binary), *args])
    if command == "destroy" and d.exists() and (d / "manifest.json").exists():
        cleanup_network(d)
    # A failed one-shot deployment remains available for root-cause inspection.
    # Explicit destroy owns controller and resource cleanup.
    return result.returncode


def _main():
    if len(sys.argv) < 2:
        raise SystemExit("Expected deploy, destroy, daemon or controller-restart")
    if sys.argv[1] == "daemon":
        daemon(Path(sys.argv[2]))
    elif sys.argv[1] == "controller-restart":
        d = state_dir(sys.argv[2])
        stop_daemon(d)
        start_daemon(d)
    else:
        raise SystemExit(cli(sys.argv[1:]))


if __name__ == "__main__":
    _main()
