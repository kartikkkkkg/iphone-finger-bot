"""Configuration load / save / validate for iphone-finger-bot.

Coordinate conventions (read README "Coordinate systems" before touching this):
  - game_region: rectangle in macOS Quartz DISPLAY POINTS, global screen space.
  - window_bounds_at_calibration: the iPhone Mirroring window bounds (points)
    at the moment of calibration. Used to detect a window move/resize and
    shift/scale the stored geometry accordingly.
  - buttons["N"]: center of button N in CAPTURE PIXELS, relative to the
    top-left corner of game_region as captured. (Capture pixels = display
    points * Retina backing scale factor.)
  - roi_size: side length (capture pixels) of the square ROI around each
    button center.
"""

import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class ConfigError(Exception):
    pass


DEFAULT_CONFIG = {
    "game_region": {"x": 0, "y": 0, "width": 0, "height": 0},
    "window_bounds_at_calibration": {"x": 0, "y": 0, "width": 0, "height": 0},
    "buttons": {str(i): {"x": 0, "y": 0} for i in range(1, 7)},
    "roi_size": 120,
    "orange_hsv": {"h_min": 10, "h_max": 24, "s_min": 90, "v_min": 90},
    "min_score": 0.22,
    "ambiguity_margin": 0.08,
}


def _require(d, key, where):
    if key not in d:
        raise ConfigError(f"config error: '{where}' is missing key '{key}'")
    return d[key]


def validate_config(cfg):
    """Raise ConfigError if the config is incomplete/invalid."""
    if not isinstance(cfg, dict):
        raise ConfigError("config must be a JSON object")

    gr = _require(cfg, "game_region", "config")
    for k in ("x", "y", "width", "height"):
        _require(gr, k, "game_region")
    if gr["width"] <= 0 or gr["height"] <= 0:
        raise ConfigError("game_region width/height must be > 0 "
                          "(run 'python bot.py --calibrate')")

    wb = _require(cfg, "window_bounds_at_calibration", "config")
    for k in ("x", "y", "width", "height"):
        _require(wb, k, "window_bounds_at_calibration")

    buttons = _require(cfg, "buttons", "config")
    for i in range(1, 7):
        b = _require(buttons, str(i), "buttons")
        bx, by = _require(b, "x", f"buttons.{i}"), _require(b, "y", f"buttons.{i}")
        if not (0 <= bx <= gr["width"] * 4 and 0 <= by <= gr["height"] * 4):
            raise ConfigError(f"buttons.{i} looks out of range: ({bx}, {by})")
        # ROI must fit inside the game region at 1x scale; at higher Retina
        # scales the capture is bigger, so this is the conservative check.
        roi = cfg.get("roi_size", 120)
        half = roi / 2.0
        if not (bx - half >= -1 and by - half >= -1):
            raise ConfigError(f"buttons.{i} ROI would extend outside the game region")

    roi_size = _require(cfg, "roi_size", "config")
    if not (16 <= roi_size <= 512):
        raise ConfigError("roi_size must be between 16 and 512")

    hsv = _require(cfg, "orange_hsv", "config")
    for k in ("h_min", "h_max", "s_min", "v_min"):
        _require(hsv, k, "orange_hsv")
    if not (0 <= hsv["h_min"] < hsv["h_max"] <= 179):
        raise ConfigError("orange_hsv hue range must satisfy 0 <= h_min < h_max <= 179")

    ms = _require(cfg, "min_score", "config")
    if not (0.0 < ms < 1.0):
        raise ConfigError("min_score must be in (0, 1)")
    am = _require(cfg, "ambiguity_margin", "config")
    if not (0.0 <= am < 1.0):
        raise ConfigError("ambiguity_margin must be in [0, 1)")

    return True


def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise ConfigError(
            f"config file not found: {path}\n"
            "Run 'python bot.py --calibrate' first."
        )
    with open(path, "r") as f:
        cfg = json.load(f)
    # Allow a "_comment" key in the template.
    cfg.pop("_comment", None)
    validate_config(cfg)
    # Detect a placeholder (never-calibrated) config: all buttons at 0,0 and
    # a zero-size region is already rejected above; this catches the template.
    if all(cfg["buttons"][str(i)]["x"] == 0 and cfg["buttons"][str(i)]["y"] == 0
           for i in range(1, 7)):
        raise ConfigError("config.json still holds placeholder values.\n"
                          "Run 'python bot.py --calibrate' first.")
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    validate_config(cfg)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=4)
        f.write("\n")
