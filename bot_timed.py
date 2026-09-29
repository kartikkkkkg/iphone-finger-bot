#!/usr/bin/env python3
"""bot_timed.py: orange-target tapper with a minimum 275 ms tap cadence.

Standalone variant of bot.py. It reuses the existing project modules
UNCHANGED — capture, detector, clicker, config, benchmark, and the ESC
watcher / TOTAL_TAPS constant from bot.py (imported, never modified).

The ONLY behavioral difference vs bot.py: a minimum click-to-click
interval (default 275 ms) is enforced between successive taps, so 10 taps
can never complete in ~2 seconds or less:
    9 intervals x 275 ms = 2.475 s  (comfortable margin above 2 s).

Timing model — the delay is applied ONLY between detection-validated
clicks, never anywhere else:
    target detected & valid & re-armed
        -> if < 275 ms since the previous click's dispatch timestamp:
               sleep exactly the remaining time (no busy-wait)
           else: click immediately
        -> dispatch click, record its timestamp

What is NOT slowed down: screen capture, HSV detection, click dispatch.
The wait is measured from the actual previous click dispatch timestamp
(taken immediately before click_button) using time.perf_counter_ns().

Usage:
    python bot_timed.py                        # 10 taps, min 275 ms cadence
    python bot_timed.py --endless              # until Ctrl+C, same cadence
    python bot_timed.py --perf-log timed.log   # perf log after the run
"""

import argparse
import sys
import threading
import time

from benchmark import Benchmark, TapRecord, now_ns
from config import load_config, ConfigError
import detector
from bot import start_esc_watcher, TOTAL_TAPS  # reuse; bot.py untouched

DEFAULT_MIN_INTERVAL_MS = 275


def wait_for_min_interval(t_last_click_ns, min_interval_ns):
    """Sleep until min_interval_ns have elapsed since t_last_click_ns.

    t_last_click_ns: perf_counter_ns() timestamp of the previous click
        dispatch, or None for the very first tap (no wait).
    Returns the total nanoseconds actually waited (0 if the game was
    already slower than the minimum interval).

    No busy-waiting: sleeps for the remaining duration, then re-checks
    the elapsed time before returning, so the minimum is guaranteed
    despite sleep overshoot/undershoot.
    """
    if t_last_click_ns is None:
        return 0
    waited_ns = 0
    while True:
        remaining_ns = min_interval_ns - (now_ns() - t_last_click_ns)
        if remaining_ns <= 0:
            return waited_ns
        t0 = now_ns()
        time.sleep(remaining_ns / 1e9)
        waited_ns += now_ns() - t0


def format_timed_report(bench, total_wait_ns, endless=False):
    """End-of-run report for bot_timed.py (default + endless modes)."""
    s = bench.summary()
    n = s["taps"]
    L = []
    L.append("================================")
    L.append("TIMED ENDLESS MODE STOPPED" if endless else
             "TIMED MODE COMPLETE")
    L.append("================================")
    L.append("")
    L.append(f"Total taps: {n}")
    if n == 0:
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
    L.append(f"Total detection + click processing: "
             f"{s['total_detect_plus_click_ms']:.2f} ms "
             f"(detect {s['total_detect_ms']:.2f} ms + "
             f"dispatch {s['total_click_dispatch_ms']:.2f} ms)")
    L.append(f"Total enforced timing wait: {total_wait_ns / 1e6:.1f} ms")
    span_label = ("First-click -> last-click duration:"
                  if endless else "First-click -> tenth-click duration:")
    L.append(f"{span_label} {s['total_game_time_s']:.3f} seconds")
    L.append("")
    L.append("================================")
    return "\n".join(L)


def run_timed(cfg, perf_log_path=None, backend="quartz", frame_budget=None,
              endless=False, min_interval_ms=DEFAULT_MIN_INTERVAL_MS,
              stats=None):
    """Timed live loop.

    frame_budget: test hook only — max loop iterations before stopping.
    min_interval_ms: minimum click-to-click interval (tests may shrink it;
        the CLI always uses 275).
    stats: optional dict filled with {"taps", "wait_ns", "report"} on exit.
    Returns 0 on clean exit.
    """
    import capture
    import clicker

    # --- Permissions: fail loudly, never silently (same as bot.py). ---
    sr = capture.check_screen_recording_permission()
    if sr is False:
        print("ERROR: Screen Recording permission is not granted.")
        print("Enable: System Settings -> Privacy & Security -> "
              "Screen Recording -> allow your Terminal/Python, then retry.")
        if capture.request_screen_recording_permission():
            print("Permission granted. Re-run the bot.")
        return 1
    ax = clicker.check_accessibility_permission()
    if ax is False:
        print("ERROR: " + clicker.accessibility_instructions().replace("\n", "\n       "))
        return 1

    cap = capture.make_capture(cfg, backend=backend)
    scale = cap.scale
    click_points = clicker.compute_click_points(cfg, cap.window["bounds"], scale)

    stop_event = threading.Event()
    start_esc_watcher(stop_event)

    bench = Benchmark()
    tap_count = 0
    armed = True          # may click when an unambiguous target is visible
    last_clicked = None   # button clicked most recently (re-arm guard)
    t_last_click_ns = None  # dispatch timestamp of the previous click
    total_wait_ns = 0
    min_interval_ns = int(min_interval_ms * 1_000_000)

    print("TIMED MODE STARTED - waiting for the first orange target...")
    print(f"Minimum tap cadence: {min_interval_ms} ms between clicks.")
    if endless:
        print("ENDLESS: no tap limit. Ctrl+C (or ESC) stops the bot.")
    else:
        print(f"Stops itself after {TOTAL_TAPS} taps. "
              "ESC or Ctrl+C stops immediately.")

    bench.mark_run_start(now_ns())
    try:
        while (endless or tap_count < TOTAL_TAPS) and not stop_event.is_set():
            if frame_budget is not None:
                if frame_budget <= 0:
                    break
                frame_budget -= 1
            t_cap = now_ns()
            frame = cap.capture_game_region()
            if frame is None:
                continue  # capture hiccup: retry immediately, no sleep

            # Detection runs at full speed — NEVER delayed by the cadence.
            t_det0 = now_ns()
            target, scores, info = detector.detect(frame, cfg)
            t_det1 = now_ns()

            if target is not None and bench.t_first_target_ns is None:
                bench.mark_first_target(t_det1)

            # Re-arm logic (identical to bot.py): after a click, wait until
            # the clicked button is no longer orange (round advanced) or a
            # different target shows.
            if not armed:
                best_i = int(scores.argmax())
                if float(scores.max()) < cfg["min_score"] or \
                        (best_i + 1) != last_clicked:
                    armed = True

            if armed and target is not None:
                # HARD SAFETY GATE (default mode): never click once 10 taps
                # are done. Endless mode intentionally has no cap.
                if not endless and tap_count >= TOTAL_TAPS:
                    break
                # Cadence enforcement: the ONLY intentional delay in this
                # script. Measured from the previous click's dispatch.
                total_wait_ns += wait_for_min_interval(t_last_click_ns,
                                                       min_interval_ns)
                t_click = now_ns()
                clicker.click_button(target, click_points)
                t_click_done = now_ns()
                t_last_click_ns = t_click
                tap_count += 1
                last_clicked = target
                armed = False
                bench.add_tap(TapRecord(tap_count, t_cap, t_det0, t_det1,
                                        t_click, t_click_done))
                if endless:
                    print(f"Tap {tap_count} -> Button {target}")
                else:
                    print(f"Tap {tap_count}/{TOTAL_TAPS} -> Button {target}")
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
    finally:
        pass
    bench.mark_run_end(now_ns())

    # --- Post-run: report + optional log. Nothing written during play. ---
    report = format_timed_report(bench, total_wait_ns, endless=endless)
    print()
    print(report)
    if perf_log_path:
        path = bench.write_log(perf_log_path, report=report)
        print(f"Performance log written to {path}")
    if stats is not None:
        stats.update({"taps": tap_count, "wait_ns": total_wait_ns,
                      "report": report})
    if endless:
        print(f"Timed endless mode stopped after {tap_count} taps. "
              "No further clicks will be sent.")
    else:
        if stop_event.is_set() and tap_count < TOTAL_TAPS:
            print(f"Stopped early after {tap_count} taps (emergency stop).")
        print("Bot stopped. No further clicks will be sent.")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        description="bot_timed.py: tap the orange button with a minimum "
                    f"{DEFAULT_MIN_INTERVAL_MS} ms tap cadence "
                    f"({TOTAL_TAPS} taps, then stop).")
    ap.add_argument("--endless", action="store_true",
                    help="no tap limit: keep playing until Ctrl+C/ESC, "
                         "still enforcing the minimum cadence")
    ap.add_argument("--perf-log", metavar="PATH", default=None,
                    help="write performance.log after the run")
    ap.add_argument("--backend", default="quartz",
                    choices=["quartz", "mss", "synthetic"],
                    help="capture backend (synthetic = testing only)")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"ERROR: {e}")
        return 1

    if args.backend == "synthetic":
        print("ERROR: --backend synthetic is for testing only, "
              "not live mode.")
        return 1
    return run_timed(cfg, perf_log_path=args.perf_log,
                     backend=args.backend, endless=args.endless,
                     min_interval_ms=DEFAULT_MIN_INTERVAL_MS)


if __name__ == "__main__":
    sys.exit(main())
