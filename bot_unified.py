#!/usr/bin/env python3
"""bot_unified.py: the unified orange-target tapper (recommended script).

Combines the functionality of bot.py and bot_timed.py in one place:

    TAP MODE                MINIMUM CLICK INTERVAL
    ----------------        ----------------------
    default: 10 taps        default: 275 ms between clicks
    --taps N: N taps        --min-interval MS: custom floor (ms)
    --endless: until Ctrl+C  --min-interval 0: no artificial floor

Existing modules (capture, detector, clicker, config, benchmark) are
reused UNCHANGED. bot.py and bot_timed.py are imported for their helpers
(ESC watcher, test/debug UI, wait helper) and are NEVER modified.

Timing model (identical to bot_timed.py):
    detect at full speed -> validate -> re-arm -> measure elapsed time
    since the actual previous click dispatch (perf_counter_ns) -> sleep
    only the remaining time if below the floor -> re-check -> click.
The first click never waits. Capture and detection are never slowed.

Run with no mode/timing flags for an interactive startup menu (mode,
tap count, interval presets, confirmation screen). Explicit --taps /
--endless / --min-interval flags bypass the menu.
"""

import argparse
import sys
import threading

from benchmark import Benchmark, TapRecord, now_ns
from config import load_config, ConfigError
import detector

import bot as bot_impl          # reused helpers; bot.py untouched
from bot_timed import wait_for_min_interval  # cadence helper; untouched

DEFAULT_TAPS = 10
DEFAULT_MIN_INTERVAL_MS = 275


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        description="bot_unified.py: tap the orange button with a "
                    "configurable tap count and a configurable minimum "
                    "click-to-click interval. Default: 10 taps, 275 ms "
                    "minimum interval.",
        epilog="""examples:
  python bot_unified.py                          # 10 taps, 275 ms cadence
  python bot_unified.py --taps 25                # 25 taps, 275 ms cadence
  python bot_unified.py --endless                # endless, 275 ms cadence
  python bot_unified.py --min-interval 300      # 10 taps, 300 ms cadence
  python bot_unified.py --min-interval 0        # 10 taps, no timing floor
  python bot_unified.py --endless --min-interval 300
  python bot_unified.py --perf-log performance.log
""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--taps", type=int, default=None, metavar="N",
                    help="stop after exactly N successful clicks "
                         f"(positive integer; default: {DEFAULT_TAPS}). "
                         "Mutually exclusive with --endless.")
    ap.add_argument("--endless", action="store_true",
                    help="no tap limit: keep playing until Ctrl+C/ESC. "
                         "Mutually exclusive with --taps.")
    ap.add_argument("--min-interval", type=float, default=None,
                    metavar="MS",
                    help="minimum click-to-click interval in milliseconds, "
                         "measured from the previous click dispatch "
                         f"(default: {DEFAULT_MIN_INTERVAL_MS}). "
                         "0 disables artificial timing enforcement. "
                         "When omitted (with no --taps/--endless), an "
                         "interactive menu asks instead.")
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


def resolve_mode(args, parser):
    """Validate tap-mode flags. Returns (mode_label, tap_limit, endless).

    tap_limit is None for endless mode. Uses parser.error() so invalid
    combinations produce a clean CLI error (exit code 2).
    """
    if args.endless and args.taps is not None:
        parser.error("--endless and --taps are mutually exclusive")
    if args.min_interval is not None and args.min_interval < 0:
        parser.error("--min-interval must be >= 0")
    if args.endless:
        return ("ENDLESS", None, True)
    if args.taps is not None:
        if args.taps < 1:
            parser.error("--taps must be a positive integer")
        return (f"{args.taps} taps", args.taps, False)
    return (f"{DEFAULT_TAPS} taps", DEFAULT_TAPS, False)


def effective_min_interval_ms(args):
    """The minimum interval to use: explicit flag, else the default."""
    return DEFAULT_MIN_INTERVAL_MS \
        if args.min_interval is None else args.min_interval


def mode_flags_explicit(args):
    """True when the user gave any mode/timing CLI flag, which means the
    interactive menu is bypassed."""
    return args.taps is not None or args.endless or args.min_interval is not None


# ---------------------------------------------------------------------------
# Interactive startup menu (only when no mode/timing flags were given)
# ---------------------------------------------------------------------------

INTERVAL_PRESETS = [
    ("275 ms (recommended)", 275.0),
    ("300 ms", 300.0),
    ("350 ms", 350.0),
    ("500 ms", 500.0),
    ("Custom", None),          # asks for a custom value
    ("No timing limit", 0.0),
]


def _ask_option(prompt, valid, default):
    """Prompt until the user picks one of `valid` (or Enter for default).

    Ctrl+C / Ctrl+D propagate to the caller, which exits cleanly.
    """
    valid = set(valid)
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default
        if raw in valid:
            return raw
        print(f"Invalid input: please enter one of "
              f"{', '.join(sorted(valid))}.")


def _ask_positive_int(prompt, default):
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default
        try:
            val = int(raw)
        except ValueError:
            print("Invalid input: please enter a positive integer.")
            continue
        if val < 1:
            print("Invalid input: please enter a positive integer.")
            continue
        return val


def _ask_non_negative_float(prompt, default):
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default
        try:
            val = float(raw)
        except ValueError:
            print("Invalid input: please enter a non-negative number.")
            continue
        if val < 0:
            print("Invalid input: please enter a non-negative number.")
            continue
        return val


def _ask_yes_no(prompt, default=True):
    while True:
        raw = input(prompt).strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Invalid input: please enter Y or n.")


def run_interactive_menu():
    """Interactive startup menu.

    Returns (mode_label, tap_limit, endless, min_interval_ms), or None when
    the user declines at the confirmation screen. Ctrl+C / Ctrl+D at any
    prompt propagate to the caller for a clean exit.
    """
    print("========================================")
    print("       iPhone Orange Button Bot")
    print("========================================")
    print()
    print("How do you want to run?")
    print()
    print("1. Limited taps")
    print("2. Endless mode")
    print()
    if _ask_option("Select mode [1/2]: ", ("1", "2"), default="1") == "1":
        taps = _ask_positive_int("How many taps? [10]: ",
                                 default=DEFAULT_TAPS)
        mode_label, tap_limit, endless = f"{taps} taps", taps, False
    else:
        mode_label, tap_limit, endless = "ENDLESS", None, True

    print()
    print("Minimum click interval:")
    print()
    for i, (label, _) in enumerate(INTERVAL_PRESETS, start=1):
        print(f"{i}. {label}")
    print()
    choice = int(_ask_option("Select interval [1]: ",
                             [str(i) for i in range(1, 7)], default="1"))
    preset_value = INTERVAL_PRESETS[choice - 1][1]
    if preset_value is None:  # Custom
        min_interval_ms = _ask_non_negative_float(
            "Custom interval in ms [275]: ",
            default=DEFAULT_MIN_INTERVAL_MS)
    else:
        min_interval_ms = preset_value

    print()
    print("========================================")
    print("Configuration")
    print("========================================")
    print()
    print(f"Mode: {'Endless' if endless else 'Limited'}")
    print(f"Taps: {'Endless' if endless else tap_limit}")
    print(f"Minimum click interval: {min_interval_ms:g} ms")
    print()
    print("========================================")
    print()
    if not _ask_yes_no("Start bot? [Y/n]: ", default=True):
        return None
    return (mode_label, tap_limit, endless, min_interval_ms)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def format_unified_report(bench, mode_label, min_interval_ms, total_wait_ns,
                          endless=False):
    """End-of-run report. Shape matches the unified spec exactly."""
    s = bench.summary()
    n = s["taps"]
    L = []
    L.append("================================")
    L.append("ENDLESS MODE STOPPED" if endless else "RUN COMPLETE")
    L.append("================================")
    L.append("")
    L.append("Mode:")
    L.append(mode_label)
    L.append("")
    L.append("Minimum click interval:")
    L.append(f"{min_interval_ms:g} ms")
    L.append("")
    L.append("Total taps:")
    L.append(f"{n}")
    if n == 0:
        L.append("")
        L.append("================================")
        return "\n".join(L)
    L.append("")
    if "total_runtime_s" in s:
        L.append("Total runtime:")
        L.append(f"{s['total_runtime_s']:.3f} seconds")
        L.append("")
    L.append("First click \u2192 last click:")
    L.append(f"{s['total_game_time_s']:.3f} seconds")
    L.append("")
    if "avg_interval_ms" in s:
        L.append("Average interval:")
        L.append(f"{s['avg_interval_ms']:.1f} ms")
        L.append("")
        L.append("Minimum interval:")
        L.append(f"{s['min_interval_ms']:.1f} ms")
        L.append("")
        L.append("Maximum interval:")
        L.append(f"{s['max_interval_ms']:.1f} ms")
        L.append("")
    L.append("Average detection time:")
    L.append(f"{s['avg_detect_ms']:.2f} ms")
    L.append("")
    L.append("Min detection:")
    L.append(f"{s['min_detect_ms']:.2f} ms")
    L.append("")
    L.append("Max detection:")
    L.append(f"{s['max_detect_ms']:.2f} ms")
    L.append("")
    L.append("Fastest detection-to-click:")
    L.append(f"{s['min_detect_to_click_ms']:.2f} ms")
    L.append("")
    L.append("Total detection + click processing:")
    L.append(f"{s['total_detect_plus_click_ms']:.2f} ms")
    L.append("")
    L.append("Total enforced timing wait:")
    L.append(f"{total_wait_ns / 1e9:.3f} seconds")
    L.append("")
    L.append("================================")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Unified live loop
# ---------------------------------------------------------------------------

def run_unified(cfg, tap_limit=DEFAULT_TAPS, endless=False,
                min_interval_ms=DEFAULT_MIN_INTERVAL_MS, debug=False,
                perf_log_path=None, backend="quartz", frame_budget=None,
                stats=None):
    """Unified live loop.

    tap_limit: stop after this many successful clicks (None when endless).
    min_interval_ms: minimum click-to-click interval; 0 disables enforcement.
    frame_budget: test hook only — max loop iterations before stopping.
    stats: optional dict filled with {"taps", "wait_ns", "report"} on exit.
    Returns 0 on clean exit.
    """
    import capture
    import clicker

    mode_label = "ENDLESS" if endless else f"{tap_limit} taps"

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
    tap_count = 0
    armed = True          # may click when an unambiguous target is visible
    last_clicked = None   # button clicked most recently (re-arm guard)
    t_last_click_ns = None  # dispatch timestamp of the previous click
    total_wait_ns = 0
    min_interval_ns = int(min_interval_ms * 1_000_000)

    print("UNIFIED MODE STARTED - waiting for the first orange target...")
    print(f"Mode: {mode_label} | Minimum click interval: "
          f"{min_interval_ms:g} ms")
    if endless:
        print("ENDLESS: no tap limit. Ctrl+C (or ESC) stops the bot.")
    else:
        print(f"Stops itself after {tap_limit} taps. "
              "ESC or Ctrl+C stops immediately.")

    if debug:
        import cv2
        cv2.namedWindow("finger-bot debug", cv2.WINDOW_NORMAL)

    bench.mark_run_start(now_ns())
    try:
        while (endless or tap_count < tap_limit) and not stop_event.is_set():
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
                # HARD SAFETY GATE (limited modes): never click once the tap
                # budget is spent. Endless mode intentionally has no cap.
                if not endless and tap_count >= tap_limit:
                    break
                # Cadence enforcement — the ONLY intentional delay. With a
                # 0 ms floor the helper returns immediately (no wait).
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
                    print(f"Tap {tap_count}/{tap_limit} -> Button {target}")

            if debug:
                bot_impl._draw_debug(frame, cfg, scores, target, tap_count,
                                     (t_det1 - t_det0) / 1e6, endless=endless)
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
    # The unified report embeds mode, minimum interval, and total enforced
    # wait, so write_log()'s existing format (report + per-tap CSV) carries
    # everything the spec requires without changing the log layout.
    report = format_unified_report(bench, mode_label, min_interval_ms,
                                   total_wait_ns, endless=endless)
    print()
    print(report)
    if perf_log_path:
        path = bench.write_log(perf_log_path, report=report)
        print(f"Performance log written to {path}")
    if stats is not None:
        stats.update({"taps": tap_count, "wait_ns": total_wait_ns,
                      "report": report})
    if endless:
        print(f"Endless mode stopped after {tap_count} taps. "
              "No further clicks will be sent.")
    else:
        if stop_event.is_set() and tap_count < tap_limit:
            print(f"Stopped early after {tap_count} taps (emergency stop).")
        print("Bot stopped. No further clicks will be sent.")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

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

    # Gameplay configuration: explicit mode/timing flags start directly;
    # otherwise show the interactive menu. Diagnostic commands (--test,
    # --verify-coords) never need gameplay configuration.
    if not (args.test or args.verify_coords):
        if mode_flags_explicit(args):
            # Validate tap-mode flags before touching config, so CLI misuse
            # (e.g. --taps 25 --endless) fails fast with a clean error.
            mode_label, tap_limit, endless = resolve_mode(args, parser)
            min_interval_ms = effective_min_interval_ms(args)
        else:
            try:
                menu = run_interactive_menu()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                return 0
            if menu is None:
                print("Exiting without starting the bot.")
                return 0
            mode_label, tap_limit, endless, min_interval_ms = menu

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

    # default: live
    if args.backend == "synthetic":
        print("ERROR: --backend synthetic is for testing only, "
              "not live mode.")
        return 1
    return run_unified(cfg, tap_limit=tap_limit, endless=endless,
                       min_interval_ms=min_interval_ms, debug=args.debug,
                       perf_log_path=args.perf_log, backend=args.backend)


if __name__ == "__main__":
    sys.exit(main())
