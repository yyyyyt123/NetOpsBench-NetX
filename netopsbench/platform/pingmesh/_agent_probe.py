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

try:
    from ._agent_support import logger
except ImportError:  # standalone in-container deployment
    from _agent_support import logger  # type: ignore[no-redef]


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
        n_df_ports (int)
        rtt_ports_per_cycle (int, optional)
        df_ports_per_cycle (int, optional)
        rtt_src_port_base (int)
        df_src_port_base (int)
        df_payload_size (int)
        burst_timeout_s (float)
    """

    def _open_burst_sockets(
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
        self._close_socket_pool(getattr(self, "_udp_df_sockets", []))
        self._udp_rtt_sockets = []
        self._udp_df_sockets = []
        self._udp_probe_src_ip = None
        self._udp_rtt_socket_cursor = 0
        self._udp_df_socket_cursor = 0

    def _ensure_udp_probe_sockets(
        self,
        src_ip: str,
        include_rtt: bool = True,
        include_df: bool | None = None,
    ) -> tuple[list, list]:
        """Create persistent source-port socket pools on first use."""
        if include_df is None:
            include_df = bool(getattr(self, "enable_df_probe", True))

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
        if not hasattr(self, "_udp_df_sockets"):
            self._udp_df_sockets = []

        if include_rtt and not self._udp_rtt_sockets:
            self._udp_rtt_sockets = self._open_burst_sockets(
                src_ip=bind_ip,
                src_port_base=self.rtt_src_port_base,
                n_ports=self.n_rtt_ports,
                set_df=True,
            )
        if include_df and not self._udp_df_sockets:
            self._udp_df_sockets = self._open_burst_sockets(
                src_ip=bind_ip,
                src_port_base=self.df_src_port_base,
                n_ports=self.n_df_ports,
                set_df=True,
            )
        return self._udp_rtt_sockets, self._udp_df_sockets

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

    def _active_socket_count(self, sockets: list, attr_name: str, fallback_attr_name: str) -> int:
        """Return the clamped number of sockets to use in this cycle."""
        if not sockets:
            return 0
        requested = getattr(self, attr_name, getattr(self, fallback_attr_name, len(sockets)))
        try:
            requested_int = int(requested)
        except (TypeError, ValueError):
            requested_int = len(sockets)
        return max(0, min(requested_int, len(sockets)))

    def _select_rotating_sockets(self, sockets: list, active_count: int, cursor_attr: str) -> list:
        """Select ``active_count`` sockets and advance a round-robin cursor."""
        if active_count <= 0 or not sockets:
            return []
        if active_count >= len(sockets):
            return list(sockets)
        cursor = int(getattr(self, cursor_attr, 0)) % len(sockets)
        selected = [sockets[(cursor + offset) % len(sockets)] for offset in range(active_count)]
        setattr(self, cursor_attr, (cursor + active_count) % len(sockets))
        return selected

    def _udp_fanout(
        self,
        probes: list[dict],
        sockets: list,
        payload_size: int,
        timeout_s: float,
    ) -> list[dict]:
        """Fan out one payload per socket/probe pair and collect echoes."""
        stats = [
            {
                "sent": 0,
                "received": 0,
                "rtts": [],
                "mtu_drops": 0,
            }
            for _probe in probes
        ]
        if not probes or not sockets:
            return stats

        payload_size = max(payload_size, _MIN_PAYLOAD_BYTES)
        padding = b"\x00" * (payload_size - _HEADER_SIZE)
        pending: dict[int, tuple[int, socket.socket, int]] = {}
        socket_pending: dict[socket.socket, int] = {}

        for _src_port, sock in sockets:
            for probe_index, probe in enumerate(probes):
                dst_ip = probe.get("dst_ip")
                if not dst_ip:
                    continue
                seq = self._next_udp_probe_seq()
                send_time_ns = time.time_ns()
                payload = struct.pack(_HEADER_FMT, send_time_ns, seq) + padding
                try:
                    sock.sendto(payload, (dst_ip, self.udp_dst_port))
                    stats[probe_index]["sent"] += 1
                    pending[seq] = (probe_index, sock, send_time_ns)
                    socket_pending[sock] = socket_pending.get(sock, 0) + 1
                except OSError as exc:
                    # EMSGSIZE (errno 90) means the kernel's cached PMTU
                    # cannot accommodate this packet with DF set; count it as
                    # a sent-but-dropped probe because it is the MTU signal.
                    if getattr(exc, "errno", None) == 90:
                        stats[probe_index]["sent"] += 1
                        stats[probe_index]["mtu_drops"] += 1
                    else:
                        logger.debug("sendto %s failed: %s", dst_ip, exc)

        deadline = time.monotonic() + timeout_s
        while pending and socket_pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readable, _, _ = select.select(list(socket_pending), [], [], remaining)
            except (OSError, ValueError):
                break
            if not readable:
                break
            for sock in readable:
                while True:
                    try:
                        data, _addr = sock.recvfrom(65535)
                    except BlockingIOError:
                        break
                    except OSError as exc:
                        logger.debug("recvfrom failed on UDP probe socket: %s", exc)
                        break
                    if len(data) < _HEADER_SIZE:
                        continue
                    sent_ns, seq = struct.unpack(_HEADER_FMT, data[:_HEADER_SIZE])
                    pending_item = pending.pop(seq, None)
                    if pending_item is None:
                        continue
                    probe_index, pending_sock, _send_time_ns = pending_item
                    count = socket_pending.get(pending_sock, 0) - 1
                    if count > 0:
                        socket_pending[pending_sock] = count
                    else:
                        socket_pending.pop(pending_sock, None)
                    rtt_ms = (time.time_ns() - sent_ns) / 1e6
                    if rtt_ms >= 0:
                        stats[probe_index]["received"] += 1
                        stats[probe_index]["rtts"].append(rtt_ms)

        return stats

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

    def udp_probe_cycle(self, probes: list[dict]) -> list[dict]:
        """Probe all destinations once using persistent source-port sockets."""
        if not probes:
            return []

        src_ip = self._probe_bind_src_ip(probes)
        rtt_sockets, df_sockets = self._ensure_udp_probe_sockets(
            src_ip=src_ip,
            include_rtt=True,
            include_df=bool(getattr(self, "enable_df_probe", True)),
        )
        rtt_active_count = self._active_socket_count(rtt_sockets, "rtt_ports_per_cycle", "n_rtt_ports")
        df_active_count = (
            self._active_socket_count(df_sockets, "df_ports_per_cycle", "n_df_ports")
            if getattr(self, "enable_df_probe", True)
            else 0
        )
        active_rtt_sockets = self._select_rotating_sockets(
            rtt_sockets,
            rtt_active_count,
            "_udp_rtt_socket_cursor",
        )
        active_df_sockets = self._select_rotating_sockets(
            df_sockets,
            df_active_count,
            "_udp_df_socket_cursor",
        )

        try:
            rtt_stats = self._udp_fanout(
                probes=probes,
                sockets=active_rtt_sockets,
                payload_size=_DEFAULT_RTT_PAYLOAD_BYTES,
                timeout_s=self.burst_timeout_s,
            )
            if getattr(self, "enable_df_probe", True):
                df_stats = self._udp_fanout(
                    probes=probes,
                    sockets=active_df_sockets,
                    payload_size=self.df_payload_size,
                    timeout_s=self.burst_timeout_s,
                )
            else:
                df_stats = []
        except Exception as exc:
            logger.warning("UDP fanout probe cycle failed: %s", exc)
            return [{"success": False, "probe": probe, "error": str(exc)} for probe in probes]

        cycle_results = []
        for probe_index, probe in enumerate(probes):
            result = self._rtt_result_from_stats(rtt_stats[probe_index], len(active_rtt_sockets))
            result["rtt_ports_active"] = len(active_rtt_sockets)
            result["rtt_ports_total"] = len(rtt_sockets)
            if getattr(self, "enable_df_probe", True):
                df_result = self._df_result_from_stats(df_stats[probe_index], len(active_df_sockets))
                result["df_success"] = 1 if df_result.get("success") else 0
                result["df_loss_pct"] = float(df_result.get("loss_pct", 100.0))
                result["df_rtt_avg"] = float(df_result.get("rtt_avg", 0.0))
                result["df_mtu_drops"] = int(df_result.get("mtu_drops", 0))
                result["df_packets_sent"] = int(df_result.get("packets_sent", 0))
                result["df_packets_lost"] = int(df_result.get("packets_lost", 0))
                result["df_ports_active"] = len(active_df_sockets)
                result["df_ports_total"] = len(df_sockets)
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

    def udp_rtt_burst(self, dst_ip: str, src_ip: str = None) -> dict:
        """Run an RTT-oriented UDP burst: many small DF packets across src_ports."""
        try:
            probe = {"src_ip": src_ip or "", "dst_ip": dst_ip}
            rtt_sockets, _df_sockets = self._ensure_udp_probe_sockets(
                src_ip=src_ip or "",
                include_rtt=True,
                include_df=False,
            )
            stats = self._udp_fanout(
                probes=[probe],
                sockets=rtt_sockets,
                payload_size=_DEFAULT_RTT_PAYLOAD_BYTES,
                timeout_s=self.burst_timeout_s,
            )
        except Exception as exc:
            logger.warning("UDP RTT burst to %s failed: %s", dst_ip, exc)
            return _empty_rtt_result(self.n_rtt_ports)

        return self._rtt_result_from_stats(stats[0], self.n_rtt_ports)

    def udp_df_burst(self, dst_ip: str, src_ip: str = None) -> dict:
        """Run a DF-oriented UDP burst: a few large DF packets across src_ports.

        A failure rate >0 here indicates either path packet loss OR an
        MTU mismatch on at least one ECMP path between src and dst.
        """
        try:
            probe = {"src_ip": src_ip or "", "dst_ip": dst_ip}
            _rtt_sockets, df_sockets = self._ensure_udp_probe_sockets(
                src_ip=src_ip or "",
                include_rtt=False,
                include_df=True,
            )
            stats = self._udp_fanout(
                probes=[probe],
                sockets=df_sockets,
                payload_size=self.df_payload_size,
                timeout_s=self.burst_timeout_s,
            )
        except Exception as exc:
            logger.warning("UDP DF burst to %s failed: %s", dst_ip, exc)
            return _empty_df_result(self.n_df_ports)

        return self._df_result_from_stats(stats[0], self.n_df_ports)
