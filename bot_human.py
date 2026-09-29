#!/usr/bin/env python3
"""bot_human.py: endless HUMANIZED orange-target tapper.

Runs until Ctrl+C like bot_delayed.py, but every tap is shaped to look
like a human playing through iPhone Mirroring instead of a machine:

  1. SPATIAL JITTER   - each tap lands at button center + a fresh 2D
     Gaussian offset (configurable sigma), clamped well inside the
     physical button. Zero-variance center-hitting is the biggest
     bot tell; humans scatter ~5-15 px.
  2. PRESS-AND-HOLD   - mouse down, hold a random 40-150 ms, mouse up.
     The stock bot posts down+up back-to-back (<1 ms); iOS observes
     touch begin/end, so a real press duration is visible.
  3. HUMAN REACTION   - the post-detection wait is drawn from a
     right-skewed (ex-Gaussian style) distribution with a human floor,
     not a flat uniform band. Fresh sample per tap.
  4. OCCASIONAL SLOW TAP - small chance per tap of an extra
     "distraction" pause, like a human blinking or hesitating.

Flow per tap:
    DETECT -> VALIDATE + RE-ARM -> REACTION WAIT -> JITTERED PRESS&HOLD
    -> CLICK -> repeat forever

Existing modules (capture, detector, clicker helpers, config,
benchmark) are reused UNCHANGED. bot.py helpers and bot_unified.py's
prompt helpers are imported and NEVER modified. clicker.py is not
touched: the hold-click is implemented locally with the same Quartz
calls clicker.py uses.

Honest caveat: the game's actual detection logic is unknown. These
changes remove the most machine-obvious signals (zero scatter, zero
hold, superhuman regularity), but no technique can guarantee passing an
unknown server-side check. More human = slower = worse at "fastest
fingers": the profiles let you choose the tradeoff.

Usage:
    python bot_human.py                        # profile menu
    python bot_human.py --perf-log human.log
"""

import argparse
import math
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

# Jitter is clamped to this fraction of the ROI half-size so taps always
# land comfortably inside the physical button.
JITTER_CLAMP_FRACTION = 0.35

PROFILES = {
    "natural": {
        "label": "Natural (recommended)",
        "desc": "most human-like; ~230-450 ms reactions",
        "delay_mean": 300.0, "delay_sd": 45.0, "delay_tau": 60.0,
        "delay_floor": 230.0,
        "jitter_sigma_px": 10.0,
        "hold_min_ms": 60.0, "hold_max_ms": 150.0,
        "slow_tap_p": 0.05, "slow_min_ms": 300.0, "slow_max_ms": 800.0,
    },
    "brisk": {
        "label": "Brisk",
        "desc": "faster; ~200-350 ms reactions",
        "delay_mean": 260.0, "delay_sd": 35.0, "delay_tau": 50.0,
        "delay_floor": 200.0,
        "jitter_sigma_px": 8.0,
        "hold_min_ms": 50.0, "hold_max_ms": 120.0,
        "slow_tap_p": 0.03, "slow_min_ms": 250.0, "slow_max_ms": 600.0,
    },
    "aggressive": {
        "label": "Aggressive",
        "desc": "near the human limit; ~180-280 ms reactions",
        "delay_mean": 225.0, "delay_sd": 25.0, "delay_tau": 40.0,
        "delay_floor": 180.0,
        "jitter_sigma_px": 6.0,
        "hold_min_ms": 40.0, "hold_max_ms": 90.0,
        "slow_tap_p": 0.02, "slow_min_ms": 200.0, "slow_max_ms": 500.0,
    },
}
PROFILE_ORDER = ["natural", "brisk", "aggressive"]


# ---------------------------------------------------------------------------
# Humanization primitives (pure, fully testable)
# ---------------------------------------------------------------------------

def sample_reaction_delay_ms(p):
    """One fresh reaction delay: ex-Gaussian style (normal + exponential
    tail), floored at a human minimum. Right-skewed like real RT data."""
    delay = (random.gauss(p["delay_mean"], p["delay_sd"])
             + random.expovariate(1.0 / p["delay_tau"]))
    return max(p["delay_floor"], delay)


def sample_hold_ms(p):
    """Press-and-hold duration for one tap."""
    return random.uniform(p["hold_min_ms"], p["hold_max_ms"])


def sample_jitter_px(p, max_radius_px):
    """Fresh 2D Gaussian offset in capture pixels, clamped to a circle so
    the tap always lands inside the button."""
    dx = random.gauss(0.0, p["jitter_sigma_px"])
    dy = random.gauss(0.0, p["jitter_sigma_px"])
    r = math.hypot(dx, dy)
    if r > max_radius_px and r > 0:
        dx *= max_radius_px / r
        dy *= max_radius_px / r
    return dx, dy


def sample_slow_extra_ms(p):
    """Occasional 'distraction' pause; 0.0 most taps."""
    if random.random() < p["slow_tap_p"]:
        return random.uniform(p["slow_min_ms"], p["slow_max_ms"])
    return 0.0


def human_click_at(x_pt, y_pt, hold_ms):
    """Native Quartz press-and-hold click at display points.

    Local implementation (clicker.py untouched): mouse down, hold, mouse
    up. Ctrl+C during the hold propagates for a clean stop.
    """
    from Quartz import (CGEventCreateMouseEvent, CGEventPost,
                        kCGEventLeftMouseDown, kCGEventLeftMouseUp,
                        kCGMouseButtonLeft, kCGHIDEventTap, CGPointMake)
    pt = CGPointMake(x_pt, y_pt)
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pt,
                                   kCGMouseButtonLeft)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pt,
                                 kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    if hold_ms > 0:
        time.sleep(hold_ms / 1000.0)
    CGEventPost(kCGHIDEventTap, up)


# ---------------------------------------------------------------------------
# Interactive profile menu
# ---------------------------------------------------------------------------

def _ask_profile_value(prompt, default, minimum=0.0):
    return _ask_non_negative_float(prompt, default=default)


def run_human_menu():
    """Profile menu. Returns (profile_name, profile_dict) or None on
    decline. Ctrl+C / Ctrl+D propagate for a clean exit."""
    print("========================================")
    print("       iPhone Orange Button Bot")
    print("            Human Mode")
    print("========================================")
    print()
    print("Humanization profile:")
    print()
    for i, key in enumerate(PROFILE_ORDER, start=1):
        p = PROFILES[key]
        print(f"{i}. {p['label']} — {p['desc']}")
    print(f"{len(PROFILE_ORDER) + 1}. Custom")
    print()
    choice = int(_ask_option("Select profile [1]: ",
                             [str(i) for i in range(1, 5)], default="1"))
    if choice <= len(PROFILE_ORDER):
        key = PROFILE_ORDER[choice - 1]
        profile = dict(PROFILES[key])
        name = key
    else:
        profile = {
            "label": "Custom",
            "desc": "user-tuned",
            "delay_mean": _ask_profile_value(
                "Reaction delay mean (ms) [300]: ", 300.0),
            "delay_sd": _ask_profile_value(
                "Reaction delay std dev (ms) [45]: ", 45.0),
            "delay_tau": _ask_profile_value(
                "Reaction delay tail (ms) [60]: ", 60.0),
            "delay_floor": _ask_profile_value(
                "Reaction delay floor (ms) [230]: ", 230.0),
            "jitter_sigma_px": _ask_profile_value(
                "Tap jitter sigma (px) [10]: ", 10.0),
            "hold_min_ms": _ask_profile_value(
                "Press-hold min (ms) [60]: ", 60.0),
            "hold_max_ms": _ask_profile_value(
                "Press-hold max (ms) [150]: ", 150.0),
            "slow_tap_p": _ask_profile_value(
                "Slow-tap probability 0-1 [0.05]: ", 0.05),
            "slow_min_ms": _ask_profile_value(
                "Slow-tap extra min (ms) [300]: ", 300.0),
            "slow_max_ms": _ask_profile_value(
                "Slow-tap extra max (ms) [800]: ", 800.0),
        }
        if profile["slow_tap_p"] > 1.0:
            print("Note: probability > 1 treated as 1 (every tap slow).")
            profile["slow_tap_p"] = 1.0
        name = "custom"

    print()
    print("========================================")
    print("Configuration")
    print("========================================")
    print()
    print("Mode: ENDLESS (humanized)")
    print(f"Profile: {profile['label']} — {profile['desc']}")
    print(f"Reaction delay: mean {profile['delay_mean']:g} ms, "
          f"floor {profile['delay_floor']:g} ms")
    print(f"Tap jitter: sigma {profile['jitter_sigma_px']:g} px")
    print(f"Press-and-hold: {profile['hold_min_ms']:g}–"
          f"{profile['hold_max_ms']:g} ms")
    print(f"Slow taps: p={profile['slow_tap_p']:g}")
    print()
    print("========================================")
    print()
    if not _ask_yes_no("Start bot? [Y/n]: ", default=True):
        return None
    return (name, profile)


# ---------------------------------------------------------------------------
# Report + performance log
# ---------------------------------------------------------------------------

def format_human_report(bench, tap_stats):
    """tap_stats: list of dicts with delay_ms, hold_ms, jx_px, jy_px,
    slow_ms per tap."""
    s = bench.summary()
    n = s["taps"]
    L = []
    L.append("========================================")
    L.append("HUMAN MODE STOPPED")
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
    delays = [t["delay_ms"] for t in tap_stats]
    holds = [t["hold_ms"] for t in tap_stats]
    offs = [math.hypot(t["jx_px"], t["jy_px"]) for t in tap_stats]
    slow_n = sum(1 for t in tap_stats if t["slow_ms"] > 0)
    total_intentional_ms = sum(delays) + sum(
        t["slow_ms"] for t in tap_stats)
    L.append("Average reaction delay:")
    L.append(f"{sum(delays) / n:.1f} ms")
    L.append("")
    L.append("Minimum reaction delay:")
    L.append(f"{min(delays):.1f} ms")
    L.append("")
    L.append("Maximum reaction delay:")
    L.append(f"{max(delays):.1f} ms")
    L.append("")
    L.append("Average press-and-hold:")
    L.append(f"{sum(holds) / n:.1f} ms")
    L.append("")
    L.append("Average tap offset from center:")
    L.append(f"{sum(offs) / n:.1f} px")
    L.append("")
    L.append("Slow taps:")
    L.append(f"{slow_n}")
    L.append("")
    L.append("Total intentional delay:")
    L.append(f"{total_intentional_ms / 1000.0:.3f} seconds")
    L.append("")
    L.append("Average detection time:")
    L.append(f"{s['avg_detect_ms']:.2f} ms")
    L.append("")
    L.append("Total detection + click processing:")
    L.append(f"{s['total_detect_plus_click_ms']:.2f} ms")
    L.append("")
    L.append("================================")
    return "\n".join(L)


def write_human_log(bench, tap_stats, path, report):
    """Performance log: report + one row per tap with the humanization
    parameters actually used. Written AFTER the run."""
    with open(path, "w") as f:
        f.write(report + "\n\n")
        f.write("tap,t_capture_ns,t_detect_start_ns,t_detect_end_ns,"
                "delay_ms,hold_ms,jitter_dx_px,jitter_dy_px,slow_extra_ms,"
                "t_click_ns,t_click_done_ns,detect_ms,"
                "detect_to_click_ms,click_dispatch_ms\n")
        for r, t in zip(bench.records, tap_stats):
            f.write(f"{r.tap},{r.t_capture_ns},{r.t_detect_start_ns},"
                    f"{r.t_detect_end_ns},{t['delay_ms']:.3f},"
                    f"{t['hold_ms']:.3f},{t['jx_px']:.2f},{t['jy_px']:.2f},"
                    f"{t['slow_ms']:.3f},{r.t_click_ns},"
                    f"{r.t_click_done_ns},{r.detect_ms:.3f},"
                    f"{r.detect_to_click_ms:.3f},"
                    f"{r.click_dispatch_ms:.3f}\n")
    return path


# ---------------------------------------------------------------------------
# Endless humanized live loop
# ---------------------------------------------------------------------------

def run_human(cfg, profile, debug=False, perf_log_path=None,
              backend="quartz", frame_budget=None, stats=None):
    """Endless humanized loop. frame_budget is a test hook only.
    stats: optional dict filled with taps/tap_stats/report on exit.
    Returns 0 on clean exit."""
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
    max_jitter_px = JITTER_CLAMP_FRACTION * cfg["roi_size"] / 2.0

    stop_event = threading.Event()
    bot_impl.start_esc_watcher(stop_event)

    bench = Benchmark()
    tap_stats = []
    tap_count = 0
    armed = True
    last_clicked = None

    print("HUMAN MODE STARTED - endless, waiting for the first "
          "orange target...")
    print(f"Profile: {profile['label']} — {profile['desc']}")
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

            t_det0 = now_ns()
            target, scores, info = detector.detect(frame, cfg)
            t_det1 = now_ns()

            if target is not None and bench.t_first_target_ns is None:
                bench.mark_first_target(t_det1)

            # Re-arm logic (identical to bot.py).
            if not armed:
                best_i = int(scores.argmax())
                if float(scores.max()) < cfg["min_score"] or \
                        (best_i + 1) != last_clicked:
                    armed = True

            if armed and target is not None:
                # Human-shaped timing: fresh samples for every tap.
                delay_ms = sample_reaction_delay_ms(profile)
                slow_ms = sample_slow_extra_ms(profile)
                hold_ms = sample_hold_ms(profile)
                jx_px, jy_px = sample_jitter_px(profile, max_jitter_px)
                if delay_ms + slow_ms > 0:
                    time.sleep((delay_ms + slow_ms) / 1000.0)
                # Jittered press-and-hold click at the scattered point.
                cx_pt, cy_pt = click_points[target]
                x_pt = cx_pt + jx_px / scale
                y_pt = cy_pt + jy_px / scale
                t_click = now_ns()
                human_click_at(x_pt, y_pt, hold_ms)
                t_click_done = now_ns()
                tap_count += 1
                last_clicked = target
                armed = False
                tap_stats.append({"delay_ms": delay_ms, "hold_ms": hold_ms,
                                  "jx_px": jx_px, "jy_px": jy_px,
                                  "slow_ms": slow_ms})
                bench.add_tap(TapRecord(tap_count, t_cap, t_det0, t_det1,
                                        t_click, t_click_done))
                print(f"Tap {tap_count} -> Button {target} "
                      f"(delay {delay_ms:.0f} ms, hold {hold_ms:.0f} ms, "
                      f"offset {math.hypot(jx_px, jy_px):.0f} px)")

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

    report = format_human_report(bench, tap_stats)
    print()
    print(report)
    if perf_log_path:
        path = write_human_log(bench, tap_stats, perf_log_path, report)
        print(f"Performance log written to {path}")
    if stats is not None:
        stats.update({"taps": tap_count, "tap_stats": list(tap_stats),
                      "report": report})
    print(f"Human mode stopped after {tap_count} taps. "
          "No further clicks will be sent.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        description="bot_human.py: endless HUMANIZED orange-target tapper. "
                    "Spatial jitter, press-and-hold, human reaction delays "
                    "and occasional slow taps. Runs until Ctrl+C.",
        epilog="""examples:
  python bot_human.py                        # profile menu
  python bot_human.py --perf-log human.log
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

    diagnostic_only = args.test or args.verify_coords
    if not diagnostic_only:
        try:
            menu = run_human_menu()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 0
        if menu is None:
            print("Exiting without starting the bot.")
            return 0
        _name, profile = menu
    else:
        profile = dict(PROFILES["natural"])

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
    return run_human(cfg, profile, debug=args.debug,
                     perf_log_path=args.perf_log, backend=args.backend)


if __name__ == "__main__":
    sys.exit(main())
