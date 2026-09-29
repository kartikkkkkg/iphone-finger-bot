"""Interactive calibration: run `python bot.py --calibrate`.

Flow:
  1. Check Screen Recording permission.
  2. Locate the iPhone Mirroring window -> game_region (full window by default).
  3. For each button 1..6: hover the mouse over its center, press Enter.
     The mouse position (Quartz points) is converted to capture pixels.
  4. ROI size prompt.
  5. Live detection sanity check on the current frame.
  6. Save config.json.

Re-run any time the iPhone Mirroring window changes size. A pure window MOVE
is handled automatically at runtime (see clicker.compute_click_points).
"""

import sys
import time

import numpy as np

from config import DEFAULT_CONFIG, save_config, ConfigError
import detector


def _die(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


def run_calibration():
    import capture
    import clicker

    print("=" * 60)
    print("iphone-finger-bot calibration")
    print("=" * 60)

    # 1. Screen Recording permission.
    perm = capture.check_screen_recording_permission()
    if perm is False:
        print("\nScreen Recording permission is NOT granted.")
        print("Enable: System Settings -> Privacy & Security -> "
              "Screen Recording -> allow your Terminal/Python.")
        if input("Open the Screen Recording settings now? [y/N] "
                 ).strip().lower() == "y":
            capture.request_screen_recording_permission()
        _die("Grant Screen Recording permission, then re-run --calibrate.")

    # 2. Find the mirroring window.
    print("\nLooking for the iPhone Mirroring window...")
    win = capture.find_iphone_mirroring_window(verbose=True)
    if win is None:
        _die("iPhone Mirroring window not found. Open iPhone Mirroring first.")
    b = win["bounds"]
    print(f"\nFound window id={win['id']} owner={win['owner']!r} "
          f"name={win['name']!r}")
    print(f"Bounds (points): x={b['x']:.0f} y={b['y']:.0f} "
          f"w={b['width']:.0f} h={b['height']:.0f}")

    cfg = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in DEFAULT_CONFIG.items()}
    cfg["buttons"] = {str(i): {"x": 0, "y": 0} for i in range(1, 7)}
    cfg["game_region"] = dict(b)
    cfg["window_bounds_at_calibration"] = dict(b)

    ans = input("\nUse the full window as the game region? [Y/n] ").strip().lower()
    if ans == "n":
        print("Enter a custom game region in display points "
              "(must contain the six buttons):")
        for k in ("x", "y", "width", "height"):
            cfg["game_region"][k] = float(input(f"  {k}: ").strip())

    # Measure the Retina scale with one capture.
    cap = capture.make_capture(cfg, backend="quartz")
    frame = cap.capture_frame()
    if frame is None:
        _die("Capture returned no image. Check Screen Recording permission.")
    scale = frame.shape[1] / b["width"] if b["width"] else 2.0
    print(f"\nMeasured backing scale: {scale:.2f} "
          f"(window image {frame.shape[1]}x{frame.shape[0]} px)")

    gr = cfg["game_region"]
    origin = (gr["x"], gr["y"])

    # 3. Button centers.
    print("\n--- Button centers ---")
    print("For each button: hover the mouse over the CENTER of the button,")
    print("then press ENTER. (The game should be visible in the mirror.)")
    for i in range(1, 7):
        input(f"\nHover over button {i} center, then press ENTER...")
        mx, my = clicker.get_mouse_position()
        px, py = clicker.screen_point_to_capture_px(origin, mx, my, scale)
        cfg["buttons"][str(i)] = {"x": float(px), "y": float(py)}
        print(f"  button {i}: mouse=({mx:.1f}, {my:.1f}) pt -> "
              f"capture=({px:.1f}, {py:.1f}) px")

    # 4. ROI size. Default 105 capture px: at typical iPhone Mirroring
    # scales this frames one button with a modest margin and keeps the six
    # ROIs disjoint. The scale-derived estimate is shown for reference only.
    scale_estimate = int(round(280 * frame.shape[1] / 1206))
    print(f"\nScale-derived estimate for reference: ~{scale_estimate} px "
          f"(from the 280 px @ 1206 px reference).")
    roi = input("ROI square size in capture pixels [105]: ").strip()
    cfg["roi_size"] = int(roi) if roi else 105

    # 5. Live sanity check.
    print("\n--- Detection sanity check ---")
    print("Capturing 5 frames and scoring the six ROIs...")
    try:
        save_config(cfg)  # validate before the check
    except ConfigError as e:
        _die(str(e))
    for k in range(5):
        f = cap.capture_game_region()
        target, scores, info = detector.detect(f, cfg)
        s = " ".join(f"{i + 1}:{v:.2f}" for i, v in enumerate(scores))
        print(f"  frame {k + 1}: target={target} scores=[{s}] {info['reason']}")
        time.sleep(0.4)
    print("\nIf a round is live you should see one button score clearly above "
          "the rest.")
    print("If scores look wrong, tune 'orange_hsv' / 'min_score' in config.json.")

    save_config(cfg)
    print(f"\nSaved calibration -> config.json")
    print("Next: run 'python bot.py --test' to verify detection (no clicking),")
    print("then 'python bot.py --verify-coords' to check click coordinates.")
    return cfg


def run_verify_coords(cfg):
    """Move (NOT click) the cursor over each calibrated button center."""
    import capture
    import clicker
    cap = capture.make_capture(cfg, backend="quartz")
    scale = cap.scale
    points = clicker.compute_click_points(cfg, cap.window["bounds"], scale)
    print("Moving the cursor over each button center (no clicks).")
    print("Visually confirm the cursor lands in the middle of each button.")
    for i in range(1, 7):
        x, y = points[i]
        print(f"  button {i}: ({x:.1f}, {y:.1f})")
        clicker.move_mouse(x, y)
        time.sleep(0.8)
    print("Done. If any position is off, re-run --calibrate.")
