"""In-memory performance benchmarking.

All timestamps are time.perf_counter_ns() (monotonic, NOT wall-clock).
Nothing is written to disk during the live loop: records are kept in memory
and flushed after the game completes.
"""

import time

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


def now_ns():
    return time.perf_counter_ns()


class TapRecord:
    __slots__ = ("tap", "t_capture_ns", "t_detect_start_ns",
                 "t_detect_end_ns", "t_click_ns", "t_click_done_ns")

    def __init__(self, tap, t_capture_ns, t_detect_start_ns,
                 t_detect_end_ns, t_click_ns, t_click_done_ns):
        self.tap = tap
        self.t_capture_ns = t_capture_ns
        self.t_detect_start_ns = t_detect_start_ns
        self.t_detect_end_ns = t_detect_end_ns
        self.t_click_ns = t_click_ns
        self.t_click_done_ns = t_click_done_ns

    @property
    def detect_ms(self):
        return (self.t_detect_end_ns - self.t_detect_start_ns) / NS_PER_MS

    @property
    def detect_to_click_ms(self):
        """Detection end -> click dispatch start (loop overhead)."""
        return (self.t_click_ns - self.t_detect_end_ns) / NS_PER_MS

    @property
    def click_dispatch_ms(self):
        """The click_button() call itself (CGEventPost round-trip)."""
        return (self.t_click_done_ns - self.t_click_ns) / NS_PER_MS


class Benchmark:
    def __init__(self):
        self.records = []
        self.t_first_target_ns = None  # first orange seen (game-start)
        self.t_first_click_ns = None
        self.t_last_click_ns = None
        self.t_run_start_ns = None     # live loop entered
        self.t_run_end_ns = None       # live loop exited

    def mark_first_target(self, t_ns):
        if self.t_first_target_ns is None:
            self.t_first_target_ns = t_ns

    def mark_run_start(self, t_ns):
        self.t_run_start_ns = t_ns

    def mark_run_end(self, t_ns):
        self.t_run_end_ns = t_ns

    def add_tap(self, rec: TapRecord):
        self.records.append(rec)
        if self.t_first_click_ns is None:
            self.t_first_click_ns = rec.t_click_ns
        self.t_last_click_ns = rec.t_click_ns

    @property
    def tap_count(self):
        return len(self.records)

    def _intervals_ms(self):
        ts = [r.t_click_ns for r in self.records]
        return [(b - a) / NS_PER_MS for a, b in zip(ts, ts[1:])]

    def summary(self):
        n = self.tap_count
        out = {"taps": n}
        if n == 0:
            return out
        intervals = self._intervals_ms()
        det = [r.detect_ms for r in self.records]
        d2c = [r.detect_to_click_ms for r in self.records]
        disp = [r.click_dispatch_ms for r in self.records]
        out.update({
            # TOTAL GAME TIME: first click -> tenth click (includes the game's
            # own render latency between rounds).
            "total_game_time_s": (self.t_last_click_ns - self.t_first_click_ns) / NS_PER_S,
            # BOT PROCESSING TIME: sum of per-tap detect (+ click dispatch).
            # Click dispatch is ~sub-ms via CGEventPost; measured per tap below.
            "avg_detect_ms": sum(det) / len(det),
            "min_detect_ms": min(det),
            "max_detect_ms": max(det),
            "min_detect_to_click_ms": min(d2c),
            "total_detect_ms": sum(det),
            "total_click_dispatch_ms": sum(disp),
            "total_detect_plus_click_ms": sum(det) + sum(disp),
        })
        if self.t_run_start_ns is not None and self.t_run_end_ns is not None:
            out["total_runtime_s"] = (
                self.t_run_end_ns - self.t_run_start_ns) / NS_PER_S
        if intervals:
            out.update({
                "avg_interval_ms": sum(intervals) / len(intervals),
                "min_interval_ms": min(intervals),
                "max_interval_ms": max(intervals),
            })
        if self.t_first_target_ns is not None:
            out["wait_for_first_target_s"] = (
                self.t_first_click_ns - self.t_first_target_ns) / NS_PER_S
        return out

    def format_report(self):
        s = self.summary()
        L = []
        L.append("================================")
        L.append("GAME COMPLETE")
        L.append("================================")
        L.append("")
        L.append(f"Total taps: {s['taps']}")
        if s["taps"] == 0:
            return "\n".join(L)
        L.append(f"Total time: {s['total_game_time_s']:.3f} seconds  "
                 "(first click -> tenth click, includes game render latency)")
        L.append(f"Average: {s['total_game_time_s'] / s['taps']:.3f} seconds/tap")
        L.append("")
        if "avg_interval_ms" in s:
            L.append(f"Minimum interval: {s['min_interval_ms']:.1f} ms")
            L.append(f"Maximum interval: {s['max_interval_ms']:.1f} ms")
            L.append(f"Average interval: {s['avg_interval_ms']:.1f} ms")
            L.append("")
        L.append(f"Average detection: {s['avg_detect_ms']:.2f} ms "
                 "(BOT PROCESSING TIME per tap)")
        L.append(f"Min detection: {s['min_detect_ms']:.2f} ms")
        L.append(f"Max detection: {s['max_detect_ms']:.2f} ms")
        if "wait_for_first_target_s" in s:
            L.append("")
            L.append(f"First-target wait before tap 1: "
                     f"{s['wait_for_first_target_s']:.3f} s")
        L.append("")
        L.append("================================")
        return "\n".join(L)

    def format_endless_report(self):
        """Ctrl+C summary for --endless mode. Untouched by default mode."""
        s = self.summary()
        L = []
        L.append("================================")
        L.append("ENDLESS MODE STOPPED")
        L.append("================================")
        L.append("")
        L.append(f"Total taps: {s['taps']}")
        if s["taps"] == 0:
            L.append("")
            L.append("================================")
            return "\n".join(L)
        if "total_runtime_s" in s:
            L.append(f"Total runtime: {s['total_runtime_s']:.3f} seconds "
                     "(loop start -> stop, includes idle waiting)")
        if "avg_interval_ms" in s:
            L.append(f"Average interval: {s['avg_interval_ms']:.1f} ms")
            L.append(f"Minimum interval: {s['min_interval_ms']:.1f} ms")
            L.append(f"Maximum interval: {s['max_interval_ms']:.1f} ms")
        L.append(f"Average detection time: {s['avg_detect_ms']:.2f} ms")
        L.append(f"Fastest detection-to-click: "
                 f"{s['min_detect_to_click_ms']:.2f} ms")
        L.append(f"Total detection + click processing: "
                 f"{s['total_detect_plus_click_ms']:.2f} ms "
                 f"(detect {s['total_detect_ms']:.2f} ms + "
                 f"dispatch {s['total_click_dispatch_ms']:.2f} ms)")
        L.append("")
        L.append("================================")
        return "\n".join(L)

    def write_log(self, path="performance.log", report=None):
        """Call AFTER the game loop. Never during the critical loop."""
        with open(path, "w") as f:
            f.write((report or self.format_report()) + "\n\n")
            f.write("tap,t_capture_ns,t_detect_start_ns,t_detect_end_ns,"
                    "t_click_ns,t_click_done_ns,detect_ms,"
                    "detect_to_click_ms,click_dispatch_ms\n")
            for r in self.records:
                f.write(f"{r.tap},{r.t_capture_ns},{r.t_detect_start_ns},"
                        f"{r.t_detect_end_ns},{r.t_click_ns},"
                        f"{r.t_click_done_ns},{r.detect_ms:.3f},"
                        f"{r.detect_to_click_ms:.3f},"
                        f"{r.click_dispatch_ms:.3f}\n")
        return path
