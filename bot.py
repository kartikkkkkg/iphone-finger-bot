#!/usr/bin/env python3
"""iphone-finger-bot: real-time orange-target tapper for iPhone Mirroring.

Usage:
    python bot.py                 # LIVE MODE: play 10 taps, then stop
    python bot.py --debug         # live mode + diagnostics window
    python bot.py --test          # detection test, NEVER clicks
    python bot.py --calibrate     # interactive calibration -> config.json
    python bot.py --verify-coords # move cursor over buttons (no clicks)
    python bot.py --list-windows  # show candidate mirroring windows
    python bot.py --perf-log PATH # write performance.log after the game

Live loop (no sleeps, no per-frame logging, no disk writes):
    CAPTURE -> DETECT -> CLICK -> repeat, max 10 clicks, then STOP.

Safety:
    - Hard tap counter: no click is ever sent when tap_count >= 10.
    - Re-arm state machine: after clicking button B the bot waits until B is
      no longer orange (or a different target appears) before it can click
      again. A single round can never be double-clicked.
    - ESC (global hotkey thread) or Ctrl+C stops immediately.
    - Single-threaded click path: no background click workers/queues.
"""

import argparse
import sys
import threading
import time

from benchmark import Benchmark, TapRecord, now_ns
from config import load_config, ConfigError
import detector

TOTAL_TAPS = 10


# ---------------------------------------------------------------------------
# Emergency stop: ESC global hotkey (macOS) + Ctrl+C
# ---------------------------------------------------------------------------

def start_esc_watcher(stop_event):
    """Daemon thread: sets stop_event on ESC keydown (keycode 53)."""
    if sys.platform != "darwin":
        return None

    def _watch():
        try:
            from Quartz import (CGEventTapCreate, CGEventTapEnable,
                                CFMachPortCreateRunLoopSource,
                                CFRunLoopAddSource, CFRunLoopRun,
                                CFRunLoopGetCurrent, kCFRunLoopCommonModes,
                                kCGSessionEventTap, kCGHeadInsertEventTap,
                                kCGEventTapOptionDefault, kCGEventKeyDown,
                                CGEventMaskBit)
        except Exception:
            return

        def _cb(proxy, etype, event, refcon):
            try:
                from Quartz import CGEventGetIntegerValueField, kCGKeyboardEventKeycode
                if CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == 53:
                    stop_event.set()
            except Exception:
                pass
            return event

        tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap,
                               kCGEventTapOptionDefault,
                               CGEventMaskBit(kCGEventKeyDown), _cb, None)
        if tap is None:
            # Event taps need Accessibility permission; Ctrl+C still works.
            return
        src = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        CFRunLoopRun()

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Live mode
# ---------------------------------------------------------------------------

def run_live(cfg, debug=False, perf_log_path=None, backend="quartz",
             frame_budget=None):
    """frame_budget: test hook only — max loop iterations before stopping."""
    import capture
    import clicker

    # --- Permissions: fail loudly, never silently. ---
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

    print("GAME STARTED - waiting for the first orange target...")
    print("(ESC or Ctrl+C stops immediately. Bot stops itself after 10 taps.)")

    if debug:
        import cv2
        cv2.namedWindow("finger-bot debug", cv2.WINDOW_NORMAL)

    try:
        while tap_count < TOTAL_TAPS and not stop_event.is_set():
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

            # Re-arm logic: after a click, wait until the clicked button is
            # no longer orange (round advanced) or a different target shows.
            if not armed:
                best_i = int(scores.argmax())
                if float(scores.max()) < cfg["min_score"] or \
                        (best_i + 1) != last_clicked:
                    armed = True

            if armed and target is not None:
                # HARD SAFETY GATE: never click once 10 taps are done.
                if tap_count >= TOTAL_TAPS:
                    break
                t_click = now_ns()
                clicker.click_button(target, click_points)
                tap_count += 1
                last_clicked = target
                armed = False
                bench.add_tap(TapRecord(tap_count, t_cap, t_det0, t_det1,
                                        t_click))
                print(f"Tap {tap_count}/{TOTAL_TAPS} -> Button {target}")

            if debug:
                _draw_debug(frame, cfg, scores, target, tap_count,
                            (t_det1 - t_det0) / 1e6)
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

    # --- Post-game: report + optional log. Nothing was written during play. ---
    print()
    print(bench.format_report())
    if perf_log_path:
        path = bench.write_log(perf_log_path)
        print(f"Performance log written to {path}")
    if stop_event.is_set() and tap_count < TOTAL_TAPS:
        print(f"Stopped early after {tap_count} taps (emergency stop).")
    print("Bot stopped. No further clicks will be sent.")
    return 0


def _draw_debug(frame, cfg, scores, target, tap_count, detect_ms):
    import cv2
    roi = cfg["roi_size"]
    for i in range(1, 7):
        # The rectangle drawn here IS the ROI extract_rois() slices:
        # roi_bounds() shares the exact centering convention.
        x0, y0, x1, y1 = detector.roi_bounds(cfg["buttons"], roi, i)
        color = (0, 165, 255) if target == i else (255, 255, 255)
        thick = 3 if target == i else 1
        cv2.rectangle(frame, (x0, y0), (x1 - 1, y1 - 1), color, thick)
        label = f"{i}" + (" TARGET" if target == i else "")
        cv2.putText(frame, label, (x0, y0 - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        cv2.putText(frame, f"{scores[i - 1]:.2f}", (x0, y1 + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    cv2.putText(frame, f"Tap {tap_count}/{TOTAL_TAPS}  detect {detect_ms:.2f} ms",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)


# ---------------------------------------------------------------------------
# Test mode: capture + detect + visualize. NEVER clicks (clicker not imported).
# ---------------------------------------------------------------------------

def run_test(cfg, backend="quartz"):
    import capture
    import cv2

    cap = capture.make_capture(cfg, backend=backend)
    cv2.namedWindow("finger-bot test (NO CLICKS)", cv2.WINDOW_NORMAL)
    print("TEST MODE - detection only. This mode NEVER clicks.")
    print("Press q or ESC in the window (or Ctrl+C) to quit.")

    last = time.perf_counter()
    try:
        while True:
            t0 = now_ns()
            frame = cap.capture_game_region()
            if frame is None:
                continue
            t1 = now_ns()
            target, scores, info = detector.detect(frame, cfg)
            t2 = now_ns()
            detect_ms = (t2 - t1) / 1e6
            now = time.perf_counter()
            fps = 1.0 / max(now - last, 1e-9)
            last = now

            vis = frame.copy()
            _draw_debug(vis, cfg, scores, target, 0, detect_ms)
            cv2.putText(vis, f"FPS: {fps:.0f}", (10, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            if target is None:
                cv2.putText(vis, f"no target ({info['reason']})", (10, 90),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2)
            cv2.imshow("finger-bot test (NO CLICKS)", vis)
            if target is not None:
                print(f"Target: {target}  Orange score: "
                      f"{scores[target - 1]:.2f}  "
                      f"Detection time: {detect_ms:.2f} ms  FPS: {fps:.0f}")
            if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                break
    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
    print("Test mode ended. Zero clicks were sent.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="iphone-finger-bot: tap the orange button, 10 rounds, "
                    "as fast as macOS allows.")
    ap.add_argument("--calibrate", action="store_true",
                    help="interactive calibration, writes config.json")
    ap.add_argument("--test", action="store_true",
                    help="detection test with debug UI; NEVER clicks")
    ap.add_argument("--debug", action="store_true",
                    help="live mode with diagnostics window")
    ap.add_argument("--verify-coords", action="store_true",
                    help="move cursor over each button (no clicks)")
    ap.add_argument("--list-windows", action="store_true",
                    help="list candidate iPhone Mirroring windows")
    ap.add_argument("--save-capture", metavar="PATH", nargs="?",
                    const="debug_capture.png", default=None,
                    help="capture one game-region frame and save it as PNG "
                         "(default: debug_capture.png); verify it visually "
                         "before any click testing")
    ap.add_argument("--perf-log", metavar="PATH", default=None,
                    help="write performance.log after the game")
    ap.add_argument("--backend", default="quartz",
                    choices=["quartz", "mss", "synthetic"],
                    help="capture backend (synthetic = testing only)")
    args = ap.parse_args(argv)

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
        print("Open it and confirm it matches the iPhone Mirroring window "
              "before any click testing.")
        return 0

    if args.calibrate:
        from calibrate import run_calibration
        run_calibration()
        return 0

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
        return run_test(cfg, backend=args.backend)

    # default: live
    if args.backend == "synthetic":
        print("ERROR: --backend synthetic is for testing only, "
              "not live mode.")
        return 1
    return run_live(cfg, debug=args.debug, perf_log_path=args.perf_log,
                    backend=args.backend)


if __name__ == "__main__":
    sys.exit(main())
