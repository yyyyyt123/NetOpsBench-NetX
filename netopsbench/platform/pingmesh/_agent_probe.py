"""UDP fanout probe helpers for the Pingmesh agent.

Replaces the previous ICMP-based ``ping``/``ping -M do`` subprocess calls
with native UDP socket fanout that varies src_port across a configurable
range. Varying src_port spreads probes across all ECMP paths in the fabric
so a fault on any single spine uplink becomes statistically visible instead
of being hidden by a fixed flow hash.

Industrial parallel: Microsoft Pingmesh / Meta NetNORAD do exactly this
— TCP/UDP probes with multiple 5-tuples — to cover every parallel link.

The receiving side runs :class:`UdpEchoResponder` from
``_agent_responder``; it echoes payloads unchanged so the sender can
recover its own send timestamp from the reply and compute RTT without
clock synchronisation.
"""

from __future__ import annotations

import select
import socket
import struct
import time

from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Linux IP_MTU_DISCOVER constants (define locally so the module is portable
# to systems whose Python build omits these socket attributes).
# ---------------------------------------------------------------------------
_IP_MTU_DISCOVER = getattr(socket, "IP_MTU_DISCOVER", 10)
_IP_PMTUDISC_DO = getattr(socket, "IP_PMTUDISC_DO", 2)

# Header layout: 8B timestamp_ns | 8B seq number | padding
_HEADER_FMT = "!QQ"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

_DEFAULT_RTT_PAYLOAD_BYTES = 64
_MIN_PAYLOAD_BYTES = _HEADER_SIZE


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    idx = min(int(len(sorted_values) * pct), len(sorted_values) - 1)
    return sorted_values[idx]


def _empty_rtt_result(sent: int) -> dict:
    return {
        "success": False,
        "rtt_min": 0.0,
        "rtt_avg": 0.0,
        "rtt_max": 0.0,
        "rtt_p90": 0.0,
        "rtt_p99": 0.0,
        "packets_sent": sent,
        "packets_lost": sent,
        "loss_pct": 100.0,
    }


def _empty_df_result(sent: int) -> dict:
    return {
        "success": False,
        "packets_sent": sent,
        "packets_lost": sent,
        "loss_pct": 100.0,
        "rtt_avg": 0.0,
        "mtu_drops": 0,
    }


class UdpProbeMixin:
    """Mixin providing UDP RTT/DF fanout probe cycles.

    Required attributes on the host class:
        udp_dst_port (int)
        n_rtt_ports (int)
        rtt_ports_per_cycle (int, optional)
        rtt_src_port_base (int)
        df_payload_size (int)
        interval (float)
    """

    def _open_probe_sockets(
        self,
        src_ip: str,
        src_port_base: int,
        n_ports: int,
        set_df: bool,
    ) -> list:
        """Open up to ``n_ports`` non-blocking UDP sockets bound to consecutive src_ports.

        Sockets that fail to bind (e.g. port already in use) are skipped.
        Returns a list of (src_port, socket) tuples.
        """
        opened: list = []
        bind_addr = src_ip or ""
        for offset in range(n_ports):
            src_port = src_port_base + offset
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            if set_df:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, _IP_MTU_DISCOVER, _IP_PMTUDISC_DO)
                except OSError:
                    logger.debug("setting IP_MTU_DISCOVER failed", exc_info=True)
            try:
                sock.bind((bind_addr, src_port))
            except OSError as exc:
                logger.debug("bind %s:%d failed: %s", bind_addr or "*", src_port, exc)
                sock.close()
                continue
            opened.append((src_port, sock))
        return opened

    def _close_socket_pool(self, sockets: list) -> None:
        for _src_port, sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    def _close_udp_probe_sockets(self) -> None:
        """Close persistent UDP probe sockets owned by this agent."""
        self._close_socket_pool(getattr(self, "_udp_rtt_sockets", []))
        self._udp_rtt_sockets = []
        self._udp_probe_src_ip = None

    def _ensure_udp_probe_sockets(
        self,
        src_ip: str,
        include_rtt: bool = True,
    ) -> list:
        """Create persistent source-port socket pools on first use."""
        bind_ip = src_ip or ""
        current_src_ip = getattr(self, "_udp_probe_src_ip", None)
        if current_src_ip is not None and current_src_ip != bind_ip:
            logger.warning(
                "UDP probe source IP changed from %s to %s; rebuilding source-port sockets",
                current_src_ip or "*",
                bind_ip or "*",
            )
            self._close_udp_probe_sockets()

        if getattr(self, "_udp_probe_src_ip", None) is None:
            self._udp_probe_src_ip = bind_ip
        if not hasattr(self, "_udp_rtt_sockets"):
            self._udp_rtt_sockets = []

        if include_rtt and not self._udp_rtt_sockets:
            self._udp_rtt_sockets = self._open_probe_sockets(
                src_ip=bind_ip,
                src_port_base=self.rtt_src_port_base,
                n_ports=self.n_rtt_ports,
                set_df=True,
            )
            if len(self._udp_rtt_sockets) != int(self.n_rtt_ports):
                opened_ports = {src_port for src_port, _sock in self._udp_rtt_sockets}
                expected_ports = set(range(self.rtt_src_port_base, self.rtt_src_port_base + self.n_rtt_ports))
                missing = sorted(expected_ports - opened_ports)
                self._close_socket_pool(self._udp_rtt_sockets)
                self._udp_rtt_sockets = []
                raise RuntimeError(f"Unable to bind the complete Pingmesh source-port pool; missing ports: {missing}")
        return self._udp_rtt_sockets

    def _next_udp_probe_seq(self) -> int:
        seq = int(getattr(self, "_udp_probe_seq", 0)) & 0xFFFFFFFFFFFFFFFF
        self._udp_probe_seq = (seq + 1) & 0xFFFFFFFFFFFFFFFF
        return seq

    def _probe_bind_src_ip(self, probes: list[dict]) -> str:
        src_ip = probes[0].get("src_ip") or ""
        for probe in probes[1:]:
            other_src_ip = probe.get("src_ip") or ""
            if other_src_ip != src_ip:
                logger.warning(
                    "Pingmesh fanout cycle received multiple src_ip values; using %s for socket bind",
                    src_ip or "*",
                )
                break
        return src_ip

    def _socket_batch(self, sockets: list, batch_index: int) -> list:
        """Select the configured, non-overlapping source-port batch."""
        if not sockets:
            return []
        batch_size = max(1, int(self.rtt_ports_per_cycle))
        batch_count = max(1, (len(sockets) + batch_size - 1) // batch_size)
        start = (int(batch_index) % batch_count) * batch_size
        return sockets[start : start + batch_size]

    @staticmethod
    def _new_probe_stats(probes: list[dict]) -> list[dict]:
        return [{"sent": 0, "received": 0, "rtts": [], "mtu_drops": 0} for _probe in probes]

    @staticmethod
    def _drain_socket(sock: socket.socket) -> None:
        """Discard replies left by a completed cycle before sockets are reused."""
        while True:
            try:
                sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError:
                return

    @staticmethod
    def _build_send_queue(probes: list[dict], rtt_sockets: list, enable_df: bool) -> list:
        """Build a stable queue where each DF probe reuses one active RTT tuple."""
        send_queue: list[tuple[str, int, socket.socket, str]] = []
        for probe_index, probe in enumerate(probes):
            dst_ip = str(probe.get("dst_ip") or "")
            if not dst_ip:
                continue
            for _src_port, rtt_socket in rtt_sockets:
                send_queue.append(("rtt", probe_index, rtt_socket, dst_ip))
            if enable_df and rtt_sockets:
                _src_port, df_socket = rtt_sockets[probe_index % len(rtt_sockets)]
                send_queue.append(("df", probe_index, df_socket, dst_ip))
        return send_queue

    def _run_udp_cycle(
        self,
        probes: list[dict],
        rtt_sockets: list,
        enable_df: bool,
    ) -> tuple[list[dict], list[dict]]:
        """Schedule RTT and DF datagrams in one non-blocking event loop."""
        rtt_stats = self._new_probe_stats(probes)
        df_stats = self._new_probe_stats(probes)
        active_sockets = [sock for _src_port, sock in rtt_sockets]
        for sock in active_sockets:
            self._drain_socket(sock)

        payloads = {
            "rtt": b"\x00" * (max(_DEFAULT_RTT_PAYLOAD_BYTES, _MIN_PAYLOAD_BYTES) - _HEADER_SIZE),
            "df": b"\x00" * (max(self.df_payload_size, _MIN_PAYLOAD_BYTES) - _HEADER_SIZE),
        }
        stats_by_kind = {"rtt": rtt_stats, "df": df_stats}
        send_queue = self._build_send_queue(probes, rtt_sockets, enable_df)

        interval = max(0.001, float(self.interval))
        cycle_started = time.monotonic()
        cycle_deadline = cycle_started + interval
        send_window = interval / 2.0
        send_spacing = send_window / max(1, len(send_queue) - 1)
        next_send_at = cycle_started
        send_index = 0
        pending: dict[int, tuple[str, int, int]] = {}

        def drain_readable(readable: list[socket.socket]) -> None:
            for readable_socket in readable:
                while True:
                    try:
                        data, _addr = readable_socket.recvfrom(65535)
                    except BlockingIOError:
                        break
                    except OSError as exc:
                        logger.debug("recvfrom failed on UDP probe socket: %s", exc)
                        break
                    if len(data) < _HEADER_SIZE:
                        continue
                    _payload_sent_ns, seq = struct.unpack(_HEADER_FMT, data[:_HEADER_SIZE])
                    pending_item = pending.pop(seq, None)
                    if pending_item is None:
                        continue
                    kind, probe_index, recorded_sent_ns = pending_item
                    rtt_ms = (time.time_ns() - recorded_sent_ns) / 1e6
                    if rtt_ms >= 0:
                        stats_by_kind[kind][probe_index]["received"] += 1
                        stats_by_kind[kind][probe_index]["rtts"].append(rtt_ms)

        while time.monotonic() < cycle_deadline and (send_index < len(send_queue) or pending):
            now = time.monotonic()
            if send_index < len(send_queue) and now >= next_send_at:
                kind, probe_index, sock, dst_ip = send_queue[send_index]
                seq = self._next_udp_probe_seq()
                send_time_ns = time.time_ns()
                payload = struct.pack(_HEADER_FMT, send_time_ns, seq) + payloads[kind]
                stats = stats_by_kind[kind][probe_index]
                try:
                    sock.sendto(payload, (dst_ip, self.udp_dst_port))
                    stats["sent"] += 1
                    pending[seq] = (kind, probe_index, send_time_ns)
                except OSError as exc:
                    if getattr(exc, "errno", None) == 90:
                        stats["sent"] += 1
                        stats["mtu_drops"] += 1
                    else:
                        logger.debug("sendto %s failed: %s", dst_ip, exc)
                send_index += 1
                next_send_at = max(next_send_at + send_spacing, time.monotonic())

            now = time.monotonic()
            wake_at = min(next_send_at, cycle_deadline) if send_index < len(send_queue) else cycle_deadline
            timeout = max(0.0, wake_at - now)
            try:
                readable, _, _ = select.select(active_sockets, [], [], timeout)
            except (OSError, ValueError):
                break
            drain_readable(readable)

        for sock in active_sockets:
            self._drain_socket(sock)
        return rtt_stats, df_stats

    def _rtt_result_from_stats(self, stats: dict, expected_sent: int) -> dict:
        sent = int(stats["sent"])
        received = int(stats["received"])
        rtts = stats["rtts"]
        mtu_drops = int(stats["mtu_drops"])
        if sent == 0:
            return _empty_rtt_result(expected_sent)

        lost = sent - received
        loss_pct = (lost / sent) * 100.0 if sent > 0 else 100.0
        if rtts:
            return {
                "success": received == sent and mtu_drops == 0,
                "rtt_min": min(rtts),
                "rtt_avg": sum(rtts) / len(rtts),
                "rtt_max": max(rtts),
                "rtt_p90": _percentile(rtts, 0.9),
                "rtt_p99": _percentile(rtts, 0.99),
                "packets_sent": sent,
                "packets_lost": lost,
                "loss_pct": loss_pct,
            }
        return {
            "success": False,
            "rtt_min": 0.0,
            "rtt_avg": 0.0,
            "rtt_max": 0.0,
            "rtt_p90": 0.0,
            "rtt_p99": 0.0,
            "packets_sent": sent,
            "packets_lost": lost,
            "loss_pct": loss_pct,
        }

    def _df_result_from_stats(self, stats: dict, expected_sent: int) -> dict:
        sent = int(stats["sent"])
        received = int(stats["received"])
        rtts = stats["rtts"]
        mtu_drops = int(stats["mtu_drops"])
        if sent == 0:
            return _empty_df_result(expected_sent)

        lost = sent - received
        loss_pct = (lost / sent) * 100.0 if sent > 0 else 100.0
        return {
            "success": received == sent and mtu_drops == 0,
            "packets_sent": sent,
            "packets_lost": lost,
            "loss_pct": loss_pct,
            "rtt_avg": (sum(rtts) / len(rtts)) if rtts else 0.0,
            "mtu_drops": mtu_drops,
        }

    def udp_probe_cycle(self, probes: list[dict], port_batch_index: int) -> list[dict]:
        """Probe all destinations once using persistent source-port sockets."""
        if not probes:
            return []

        src_ip = self._probe_bind_src_ip(probes)
        rtt_sockets = self._ensure_udp_probe_sockets(
            src_ip=src_ip,
            include_rtt=True,
        )
        active_rtt_sockets = self._socket_batch(rtt_sockets, port_batch_index)
        enable_df = bool(getattr(self, "enable_df_probe", True))

        try:
            rtt_stats, df_stats = self._run_udp_cycle(
                probes,
                active_rtt_sockets,
                enable_df,
            )
        except Exception as exc:
            logger.warning("UDP fanout probe cycle failed: %s", exc)
            return [{"success": False, "probe": probe, "error": str(exc)} for probe in probes]

        cycle_results = []
        for probe_index, probe in enumerate(probes):
            result = self._rtt_result_from_stats(rtt_stats[probe_index], len(active_rtt_sockets))
            result["rtt_ports_active"] = len(active_rtt_sockets)
            result["rtt_ports_total"] = len(rtt_sockets)
            result["port_batch_index"] = int(port_batch_index)
            if enable_df:
                expected_df_packets = 1 if active_rtt_sockets else 0
                df_result = self._df_result_from_stats(df_stats[probe_index], expected_df_packets)
                result["df_success"] = 1 if df_result.get("success") else 0
                result["df_loss_pct"] = float(df_result.get("loss_pct", 100.0))
                result["df_rtt_avg"] = float(df_result.get("rtt_avg", 0.0))
                result["df_mtu_drops"] = int(df_result.get("mtu_drops", 0))
                result["df_packets_sent"] = int(df_result.get("packets_sent", 0))
                result["df_packets_lost"] = int(df_result.get("packets_lost", 0))
                result["df_ports_active"] = expected_df_packets
                result["df_ports_total"] = len(rtt_sockets)
            else:
                result["df_success"] = 0
                result["df_loss_pct"] = 0.0
                result["df_rtt_avg"] = 0.0
                result["df_mtu_drops"] = 0
                result["df_packets_sent"] = 0
                result["df_packets_lost"] = 0
                result["df_ports_active"] = 0
                result["df_ports_total"] = 0
            cycle_results.append({"success": True, "probe": probe, "result": result})
        return cycle_results
