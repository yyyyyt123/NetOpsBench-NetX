"""Runtime loop helpers for Pingmesh agent."""

from __future__ import annotations

try:
    from ._agent_support import logger, time
except ImportError:
    from _agent_support import logger, time


class PingRuntimeMixin:
    def _write_probe_result(self, probe: dict, result: dict) -> None:
        self.write_metrics(probe, result)
        if result["packets_lost"] > 0:
            self.write_drop_log(probe, result)

    def run(self):
        logger.info("Starting probe loop")
        logger.info("  Probe worker: 1")
        if self.min_interval == self.max_interval:
            logger.info("  Fixed cycle interval: %ss", self.min_interval)
        else:
            logger.info("  Interval range: %s-%ss", self.min_interval, self.max_interval)
        startup_jitter = float(getattr(self, "startup_jitter_s", 0.0) or 0.0)
        if startup_jitter > 0:
            logger.info("  Startup jitter sleep: %.3fs", startup_jitter)
            time.sleep(startup_jitter)
        while True:
            start = time.time()
            completed = 0
            failed = 0
            try:
                cycle_results = self.udp_probe_cycle(self.tasks)
            except Exception as exc:
                logger.warning("Unexpected probe cycle error: %s", exc)
                cycle_results = []
                failed = len(self.tasks)
            for item in cycle_results:
                probe = item.get("probe", {})
                if item.get("success"):
                    try:
                        self._write_probe_result(probe, item["result"])
                        completed += 1
                    except Exception as exc:
                        logger.warning(
                            "Probe write failed %s→%s: %s",
                            probe.get("src_name", "?"),
                            probe.get("dst_name", "?"),
                            exc,
                        )
                        failed += 1
                else:
                    logger.warning(
                        "Probe failed %s→%s: %s",
                        probe.get("src_name", "?"),
                        probe.get("dst_name", "?"),
                        item.get("error", "unknown error"),
                    )
                    failed += 1
            elapsed = time.time() - start
            target_interval = min(max(self.min_interval, elapsed * 1.5), self.max_interval)
            sleep_time = max(0, target_interval - elapsed)
            logger.info(
                "Cycle complete: %s succeeded, %s failed, %.1fs elapsed, sleeping %.1fs",
                completed,
                failed,
                elapsed,
                sleep_time,
            )
            time.sleep(sleep_time)

    def shutdown(self):
        logger.info("Shutting down agent, flushing remaining metrics...")
        self.shutdown_event.set()
        close_probe_sockets = getattr(self, "_close_udp_probe_sockets", None)
        if callable(close_probe_sockets):
            close_probe_sockets()
        responder = getattr(self, "responder", None)
        if responder is not None:
            try:
                responder.stop()
            except Exception as exc:
                logger.warning("UDP responder shutdown error: %s", exc)
        self.write_queue.join()
        with self.batch_lock:
            if self.batch_buffer:
                self._write_to_influxdb(self.batch_buffer)
                self.batch_buffer = []
        if self.session:
            self.session.close()
