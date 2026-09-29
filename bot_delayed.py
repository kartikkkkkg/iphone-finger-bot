#!/usr/bin/env python3
"""bot_delayed.py: endless delayed-click orange-target tapper.

This script has NO limited tap mode and NO --taps option. It runs
continuously until Ctrl+C. Its only gameplay parameter is a random delay
applied AFTER a valid orange target is detected and BEFORE the click:

    DETECT ORANGE -> VALIDATE + RE-ARM -> RANDOM WAIT -> CLICK -> repeat

This is NOT a minimum click-to-click interval (see bot_timed.py /
bot_unified.py for that). Detection keeps running at full speed until a
valid target appears; the wait happens strictly between detection and
the click dispatch.

Existing modules (capture, detector, clicker, config, benchmark) are
reused UNCHANGED. bot.py helpers (ESC watcher, test/debug UI) and
bot_unified.py's prompt helpers are imported and NEVER modified.

Usage:
    python bot_delayed.py                        # delay-selection menu
    python bot_delayed.py --perf-log delayed.log # with performance logging
"""

import argparse
import random
import sys
import threading
import time

from benchmark import Benchmark, TapRecord, now_ns
from config import load_config, ConfigError
import detector

import bot as bot_impl  # reused helpers; bot.py untouched
from bot_unified import (_ask_option, _ask_yes_no,  # prompt helpers
                         _ask_non_negative_float)   # bot_unified.py untouched

DEFAULT_MIN_DELAY_MS = 150.0
DEFAULT_MAX_DELAY_MS = 200.0

DELAY_PRESETS = [
    ("150\u2013200 ms", 150.0, 200.0),
    ("150\u2013250 ms", 150.0, 250.0),
    ("200\u2013250 ms", 200.0, 250.0),
    ("Custom range", None, None),
    ("No delay", 0.0, 0.0),
]


# ---------------------------------------------------------------------------
# Delay generation / waiting
# ---------------------------------------------------------------------------

def generate_delay_ms(min_delay_ms, max_delay_ms):
    """Generate one fresh random delay in [min_delay_ms, max_delay_ms].

    If both are 0 (or max <= 0), returns 0.0 -> no intentional waiting.
    """
    if max_delay_ms <= 0:
        return 0.0
    return random.uniform(min_delay_ms, max_delay_ms)


def wait_delay_ms(delay_ms):
    """Sleep for the generated delay (no busy-wait). Returns actual ms.

    Ctrl+C during the sleep propagates to the caller for a clean stop.
    """
    if delay_ms <= 0:
        return 0.0
    t0 = now_ns()
    time.sleep(delay_ms / 1000.0)
    return (now_ns() - t0) / 1e6


def delay_label(min_delay_ms, max_delay_ms):
    if max_delay_ms <= 0:
        return "No delay"
    if min_delay_ms == max_delay_ms:
        return f"{min_delay_ms:g} ms"
    return f"{min_delay_ms:g}\u2013{max_delay_ms:g} ms"


# ---------------------------------------------------------------------------
# Interactive delay-selection menu (no tap-count questions, ever)
# ---------------------------------------------------------------------------

def run_delay_menu():
    """Delay-selection menu. Returns (min_delay_ms, max_delay_ms, label),
    or None when the user declines at the confirmation screen.
    Ctrl+C / Ctrl+D at any prompt propagate for a clean exit."""
    print("========================================")
    print("       iPhone Orange Button Bot")
    print("           Delayed Mode")
    print("========================================")
    print()
    print("Delay after detecting orange:")
    print()
    for i, (label, _, _) in enumerate(DELAY_PRESETS, start=1):
        print(f"{i}. {label}")
    print()
    choice = int(_ask_option("Select option [1]: ",
                             [str(i) for i in range(1, 6)], default="1"))
    _, lo, hi = DELAY_PRESETS[choice - 1]
    if lo is None:  # Custom range
        while True:
            lo = _ask_non_negative_float("Minimum delay (ms): ",
                                         default=DEFAULT_MIN_DELAY_MS)
            hi = _ask_non_negative_float("Maximum delay (ms): ",
                                         default=DEFAULT_MAX_DELAY_MS)
            if lo > hi:
                print("Invalid input: minimum delay must be "
                      "<= maximum delay.")
                continue
            break
    label = delay_label(lo, hi)

    print()
    print("========================================")
    print("Configuration")
    print("========================================")
    print()
    print("Mode: ENDLESS")
    print(f"Delay after detection: {label}")
    print()
    print("========================================")
    print()
    if not _ask_yes_no("Start bot? [Y/n]: ", default=True):
        return None
    return (lo, hi, label)


# ---------------------------------------------------------------------------
# Report + performance log
# ---------------------------------------------------------------------------

def format_delayed_report(bench, delays, label):
    """End-of-run report for the endless delayed bot."""
    s = bench.summary()
    n = s["taps"]
    total_delay_ms = sum(delays)
    L = []
    L.append("========================================")
    L.append("ENDLESS DELAYED BOT STOPPED")
    L.append("========================================")
    L.append("")
    L.append("Total taps:")
    L.append(f"{n}")
    if n == 0:
        L.append("")
        L.append("========================================")
        return "\n".join(L)
    L.append("")
    if "total_runtime_s" in s:
        L.append("Total runtime:")
        L.append(f"{s['total_runtime_s']:.3f} seconds")
        L.append("")
    L.append("Average delay after detection:")
    L.append(f"{total_delay_ms / n:.6f} ms")
    L.append("")
    L.append("Minimum generated delay:")
    L.append(f"{min(delays):.6f} ms")
    L.append("")
    L.append("Maximum generated delay:")
    L.append(f"{max(delays):.6f} ms")
    L.append("")
    L.append("Total intentional delay:")
    L.append(f"{total_delay_ms / 1000.0:.6f} seconds")
    L.append("")
    L.append("Average detection time:")
    L.append(f"{s['avg_detect_ms']:.2f} ms")
    L.append("")
    L.append("Total detection + click processing:")
    L.append(f"{s['total_detect_plus_click_ms']:.2f} ms")
    L.append("")
    L.append("========================================")
    return "\n".join(L)


def write_delayed_log(bench, delays, path, report):
    """Performance log: report + one row per tap including the generated
    delay. Written AFTER the run, never during the critical loop."""
    with open(path, "w") as f:
        f.write(report + "\n\n")
        f.write("tap,t_capture_ns,t_detect_start_ns,t_detect_end_ns,"
                "delay_ms,t_click_ns,t_click_done_ns,detect_ms,"
                "detect_to_click_ms,click_dispatch_ms\n")
        for r, d in zip(bench.records, delays):
            f.write(f"{r.tap},{r.t_capture_ns},{r.t_detect_start_ns},"
                    f"{r.t_detect_end_ns},{d:.3f},{r.t_click_ns},"
                    f"{r.t_click_done_ns},{r.detect_ms:.3f},"
                    f"{r.detect_to_click_ms:.3f},"
                    f"{r.click_dispatch_ms:.3f}\n")
    return path


# ---------------------------------------------------------------------------
# Endless delayed live loop
# ---------------------------------------------------------------------------

def run_delayed(cfg, min_delay_ms=DEFAULT_MIN_DELAY_MS,
                max_delay_ms=DEFAULT_MAX_DELAY_MS, debug=False,
                perf_log_path=None, backend="quartz", frame_budget=None,
                stats=None):
    """Endless delayed-click loop. frame_budget is a test hook only.
    stats: optional dict filled with taps/delays/report on exit.
    Returns 0 on clean exit."""
    import capture
    import clicker

    label = delay_label(min_delay_ms, max_delay_ms)

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
    bot_impl.start_esc_watcher(stop_event)

    bench = Benchmark()
    delays = []           # generated delay per tap, in ms
    tap_count = 0
    armed = True          # may click when an unambiguous target is visible
    last_clicked = None   # button clicked most recently (re-arm guard)

    print("DELAYED MODE STARTED - endless, waiting for the first "
          "orange target...")
    print(f"Delay after detection: {label}")
    print("Ctrl+C (or ESC) stops the bot.")

    if debug:
        import cv2
        cv2.namedWindow("finger-bot debug", cv2.WINDOW_NORMAL)

    bench.mark_run_start(now_ns())
    try:
        while not stop_event.is_set():
            if frame_budget is not None:
                if frame_budget <= 0:
                    break
                frame_budget -= 1
            t_cap = now_ns()
            frame = cap.capture_game_region()
            if frame is None:
                continue  # capture hiccup: retry immediately, no sleep

            # Detection runs at full speed until a valid target appears.
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
                # DETECTION -> RANDOM WAIT -> CLICK. The delay is generated
                # fresh for every click and happens strictly before it.
                delay_ms = generate_delay_ms(min_delay_ms, max_delay_ms)
                delays.append(delay_ms)
                wait_delay_ms(delay_ms)
                t_click = now_ns()
                clicker.click_button(target, click_points)
                t_click_done = now_ns()
                tap_count += 1
                last_clicked = target
                armed = False
                bench.add_tap(TapRecord(tap_count, t_cap, t_det0, t_det1,
                                        t_click, t_click_done))
                print(f"Tap {tap_count} -> Button {target} "
                      f"(delay {delay_ms:.6f} ms)")

            if debug:
                bot_impl._draw_debug(frame, cfg, scores, target, tap_count,
                                     (t_det1 - t_det0) / 1e6, endless=True)
                import cv2
                cv2.imshow("finger-bot debug", frame)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break
    except KeyboardInterrupt:
        print("\nStopped by user (Ctrl+C).")
    finally:
        if debug:
            import cv2
            cv2.destroyAllWindows()
    bench.mark_run_end(now_ns())

    # --- Post-run: report + optional log. Nothing written during play. ---
    report = format_delayed_report(bench, delays, label)
    print()
    print(report)
    if perf_log_path:
        path = write_delayed_log(bench, delays, perf_log_path, report)
        print(f"Performance log written to {path}")
    if stats is not None:
        stats.update({"taps": tap_count, "delays": list(delays),
                      "report": report})
    print(f"Endless delayed bot stopped after {tap_count} taps. "
          "No further clicks will be sent.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        description="bot_delayed.py: endless delayed-click orange-target "
                    "tapper. No tap limit, no --taps: detect orange -> "
                    "random delay -> click, until Ctrl+C.",
        epilog="""examples:
  python bot_delayed.py                        # delay-selection menu
  python bot_delayed.py --perf-log delayed.log
""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--perf-log", metavar="PATH", default=None,
                    help="write performance.log after the run")
    ap.add_argument("--debug", action="store_true",
                    help="live mode with diagnostics window")
    ap.add_argument("--verify-coords", action="store_true",
                    help="move cursor over each button (no clicks)")
    ap.add_argument("--list-windows", action="store_true",
                    help="list candidate iPhone Mirroring windows")
    ap.add_argument("--test", action="store_true",
                    help="detection test with debug UI; NEVER clicks")
    ap.add_argument("--save-capture", metavar="PATH", nargs="?",
                    const="debug_capture.png", default=None,
                    help="capture one game-region frame and save it as PNG "
                         "(default: debug_capture.png)")
    ap.add_argument("--calibrate", action="store_true",
                    help="interactive calibration, writes config.json")
    ap.add_argument("--backend", default="quartz",
                    choices=["quartz", "mss", "synthetic"],
                    help="capture backend (synthetic = testing only)")
    return ap


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_windows:
        import capture
        win = capture.find_iphone_mirroring_window(verbose=True)
        print("selected:", win)
        return 0

    if args.save_capture:
        import capture
        import cv2
        try:
            cfg0 = load_config()
        except ConfigError as e:
            print(f"ERROR: {e}")
            return 1
        cap = capture.make_capture(cfg0, backend=args.backend)
        frame = cap.capture_game_region()
        if frame is None:
            print("ERROR: capture returned no image.")
            return 1
        cv2.imwrite(args.save_capture, frame)
        print(f"Saved {frame.shape[1]}x{frame.shape[0]} frame -> "
              f"{args.save_capture}")
        return 0

    if args.calibrate:
        from calibrate import run_calibration
        run_calibration()
        return 0

    # Diagnostic commands never start the endless bot and never show the
    # delay menu.
    diagnostic_only = args.test or args.verify_coords
    if not diagnostic_only:
        try:
            menu = run_delay_menu()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 0
        if menu is None:
            print("Exiting without starting the bot.")
            return 0
        min_delay_ms, max_delay_ms, _label = menu
    else:
        min_delay_ms, max_delay_ms = DEFAULT_MIN_DELAY_MS, DEFAULT_MAX_DELAY_MS

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"ERROR: {e}")
        return 1

    if args.verify_coords:
        from calibrate import run_verify_coords
        run_verify_coords(cfg)
        return 0

    if args.test:
        return bot_impl.run_test(cfg, backend=args.backend)

    if args.backend == "synthetic":
        print("ERROR: --backend synthetic is for testing only, "
              "not live mode.")
        return 1
    return run_delayed(cfg, min_delay_ms=min_delay_ms,
                       max_delay_ms=max_delay_ms, debug=args.debug,
                       perf_log_path=args.perf_log, backend=args.backend)


if __name__ == "__main__":
    sys.exit(main())
