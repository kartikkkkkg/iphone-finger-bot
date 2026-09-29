# iPhone Finger Bot

A real-time, high-performance macOS automation project that plays a
"Fastest Fingers First" reaction game running on an iPhone, viewed live
through Apple's **iPhone Mirroring** on a Mac.

This is **not** an AI agent. There is no LLM, no vision API, no cloud
service, no OCR, and no object-detection model anywhere in the gameplay
path. The bot is a small, deterministic, local computer-vision loop:

```
iPhone
  ↓  (iPhone Mirroring)
macOS window capture (Quartz CoreGraphics)
  ↓
game-region crop
  ↓
6 fixed button ROIs
  ↓
HSV orange segmentation (vectorized NumPy/OpenCV)
  ↓
6 scores → argmax + threshold + ambiguity margin
  ↓
native CGEvent mouse click at the calibrated point
  ↓
repeat — exactly 10 times, then STOP
```

The entire design optimizes one metric: **end-to-end latency per tap**.

## Game mechanics

- 10 rounds per game. After the 10th tap the bot **must stop** — never an 11th click.
- Each round shows 6 circular buttons in a fixed 3×2 grid. Five are purple, exactly one turns orange.
- The bot waits on the "STARTING SOON" screen until an orange target appears, taps it, and repeats.
- Button positions never move; only the orange target changes.

## Requirements

- macOS (Apple Silicon recommended)
- Python 3.10+
- iPhone Mirroring with the game visible
- **Accessibility** permission — for synthetic mouse clicks
- **Screen Recording** permission — for window capture

## Installation

```bash
git clone https://github.com/kartikkkkkg/iphone-finger-bot.git
cd iphone-finger-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## macOS permissions

The bot checks both permissions at startup and **fails loudly** instead of
silently misbehaving.

**Accessibility** (required for clicks):

> System Settings → Privacy & Security → Accessibility → `+` → add your
> Terminal app (or the Python interpreter) and toggle it ON.

**Screen Recording** (required for capture):

> System Settings → Privacy & Security → Screen Recording → allow your
> Terminal app / Python.

`python bot.py --list-windows` is a quick way to confirm capture works: it
prints the detected iPhone Mirroring window.

## Calibration

Run once (again only if the mirroring window changes *size* — a pure window
*move* is handled automatically):

```bash
python bot.py --calibrate
```

The wizard:

1. Finds the iPhone Mirroring window → sets the game region (full window by default).
2. Measures the Retina backing scale from a live capture.
3. For each button 1–6: hover the mouse over the button center, press ENTER.
4. Prompts for the ROI size (default 105 capture px: frames one button with
   a modest margin while keeping the six ROIs disjoint).
5. Runs a live detection sanity check and saves `config.json`.

Verify click coordinates without clicking anything:

```bash
python bot.py --verify-coords   # moves the cursor over each button, no clicks
```

### Coordinate systems

Three spaces are involved; mixing them up is the classic bug:

| Space | Unit | Used for |
|---|---|---|
| Quartz display points | pt | window bounds, mouse events |
| Capture pixels | px | frames from `CGWindowListCreateImage` (= pt × Retina scale) |
| ROI pixels | px | button centers relative to game-region origin |

`clicker.capture_px_to_screen_point()` / `screen_point_to_capture_px()`
convert between them, and `compute_click_points()` additionally compensates
for the mirroring window having moved since calibration. The scale is
**measured** from every capture (`image_width_px / window_width_pt`), never
assumed.

## Detection test (never clicks)

```bash
python bot.py --test
```

Captures the live game, scores the six ROIs, and shows a debug window with
each ROI boxed, the detected target highlighted, plus target / orange score /
detection time / FPS. **The `clicker` module is not even imported in this
mode — a click is structurally impossible.**

## Live mode

```bash
python bot.py
```

Sequence: load config → validate permissions → init capture → compute click
points → wait for orange → detect → click → repeat → stop after tap 10.

```
GAME STARTED - waiting for the first orange target...
Tap 1/10 -> Button 4
Tap 2/10 -> Button 2
...
Tap 10/10 -> Button 1

================================
GAME COMPLETE
================================
...
Bot stopped. No further clicks will be sent.
```

Debug mode adds a live diagnostics window:

```bash
python bot.py --debug
```

## Timed mode (minimum 275 ms tap cadence)

`bot_timed.py` is a standalone variant of `bot.py` (which it leaves
completely untouched). It runs the identical capture → detect → click
pipeline with the identical detector, ROI calibration, native clicks,
and re-arm protection — but enforces a **minimum 275 ms interval between
successive clicks**, so 10 taps can never complete in ~2 seconds or less
(9 intervals × 275 ms = 2.475 s, a comfortable margin above 2 s).

Normal (10 taps, then stop):

```bash
python bot_timed.py
```

Endless (no tap limit, until Ctrl+C — same 275 ms cadence):

```bash
python bot_timed.py --endless
```

Performance logging (same mechanism as `bot.py --perf-log`):

```bash
python bot_timed.py --perf-log performance.log
```

How the timing works:

- Capture and HSV detection always run at full speed — the cadence adds
  **zero** delay to capture, detection, or click dispatch.
- When a target is valid and the bot is re-armed, it checks the time
  since the **actual previous click dispatch timestamp**
  (`time.perf_counter_ns()`, monotonic, high resolution).
- If less than 275 ms has passed, it sleeps only the remaining time
  (no busy-waiting), then re-checks before clicking. If the game is
  slower than 275 ms, nothing extra is added — the click fires
  immediately.
- At the end of a 10-tap run it reports: total taps, total runtime,
  average/minimum/maximum interval, average detection time, total
  detection + click processing time, **total enforced timing wait**,
  and the actual first-click → tenth-click duration. Endless mode prints
  the same statistics on Ctrl+C.

## Unified Bot (recommended)

`bot_unified.py` combines `bot.py` and `bot_timed.py` into one script —
the same capture, detector, ROI calibration, native clicks, and re-arm
protection, with two independent choices: **tap mode** and **minimum
click interval**. The older scripts remain untouched for backward
compatibility.

```bash
# Default: 10 taps, 275 ms minimum interval
python bot_unified.py

# 25 taps
python bot_unified.py --taps 25

# Endless mode
python bot_unified.py --endless

# 10 taps with 300 ms minimum interval
python bot_unified.py --min-interval 300

# 10 taps with no artificial timing floor
python bot_unified.py --min-interval 0

# Endless with 300 ms minimum interval
python bot_unified.py --endless --min-interval 300

# Performance logging
python bot_unified.py --perf-log performance.log
```

- Tap mode: default 10 taps (same hard safety limit as `bot.py`),
  `--taps N` for any positive count, `--endless` until Ctrl+C.
  `--taps` and `--endless` are mutually exclusive.
- Minimum interval: default 275 ms (9 intervals × 275 ms = 2475 ms from
  click #1 to click #10), measured from the actual previous click
  dispatch via `time.perf_counter_ns()`. Detection and capture always
  run at full speed; when the game is slower than the interval, clicks
  fire immediately with zero added delay.
- `--debug`, `--test`, `--verify-coords`, `--calibrate`,
  `--list-windows`, and `--save-capture` work as in `bot.py`.

### Interactive startup menu

Run with no mode/timing flags and `bot_unified.py` walks you through the
setup — mode, tap count, interval preset, and a confirmation screen:

```bash
python bot_unified.py
```

```
========================================
       iPhone Orange Button Bot
========================================

How do you want to run?

1. Limited taps
2. Endless mode

Select mode [1/2]:

How many taps? [10]:

Minimum click interval:

1. 275 ms (recommended)
2. 300 ms
3. 350 ms
4. 500 ms
5. Custom
6. No timing limit

Select interval [1]:

========================================
Configuration
========================================

Mode: Limited
Taps: 10
Minimum click interval: 275 ms

========================================

Start bot? [Y/n]:
```

Any explicit `--taps`, `--endless`, or `--min-interval` flag skips the
menu entirely. Diagnostic commands (`--test`, `--verify-coords`,
`--calibrate`, `--list-windows`) never show it. Ctrl+C at any prompt
exits cleanly; invalid input is rejected with an error and asked again —
nothing is ever chosen silently.

## Endless mode

```bash
python bot.py --endless
```

Same loop as live mode — capture → detect → click with the identical
re-arm guard — but **no 10-tap cap**: it keeps playing until you stop it
with Ctrl+C (or ESC). On interrupt it exits cleanly and prints a session
summary:

```
================================
ENDLESS MODE STOPPED
================================

Total taps: 47
Total runtime: 83.412 seconds (loop start -> stop, includes idle waiting)
Average interval: 1781.2 ms
Minimum interval: 1654.0 ms
Maximum interval: 1922.7 ms
Average detection time: 0.42 ms
Fastest detection-to-click: 0.18 ms
Total detection + click processing: 24.31 ms (detect 19.74 ms + dispatch 4.57 ms)

================================
Endless mode stopped after 47 taps. No further clicks will be sent.
```

`--perf-log` works in endless mode too (`python bot.py --endless --perf-log
performance.log`); the log header is the endless report above, followed by
the per-tap CSV (now including `t_click_done_ns`, `detect_to_click_ms`,
`click_dispatch_ms` columns).

## Safety: exactly 10 clicks (default mode)

- A hard `tap_count < 10` gate wraps **every** click dispatch, plus a
  `break` the moment the 10th tap lands. `--endless` is the only mode that
  bypasses this cap, and only because the user explicitly asked for it on
  the command line.
- A re-arm state machine prevents double-tapping one round: after clicking
  button B, the bot cannot click again until B stops being orange (round
  advanced) or a *different* target appears.
- Single-threaded click path — no background workers, no click queues that
  could outlive the main loop.
- No click is ever sent before an orange target is detected.
- Emergency stop: **ESC** (global hotkey) or **Ctrl+C** stops immediately.

## False-positive protection

- Only the six button ROIs are ever analyzed. Orange UI anywhere else on
  screen (gold titles, timers, gift boxes) **cannot** trigger a click.
- A target needs `score >= min_score` (default 0.22).
- The winner must beat the runner-up by `ambiguity_margin` (default 0.08),
  otherwise the bot keeps capturing instead of guessing.
- No `time.sleep()` anywhere in the live loop — waiting is done by
  capturing the next frame immediately.

## Performance

### What controls detection speed

`detector.py`: one `cvtColor` + one `inRange` over a single stacked
`(6, H, W, 3)` array of the six ROIs (no per-ROI conversions, no Python
pixel loops), then six `countNonZero`s. Measured ~2 ms on 105 px ROIs
(the default; smaller than the earlier 280 px reference ROIs, so the
current build is faster).
(`orange_hsv` in `config.json`) do not affect speed.

### What controls click speed

`clicker.py`: `CGEventCreateMouseEvent` + `CGEventPost(kCGHIDEventTap)` —
down and up posted back-to-back with no delay. This is the lowest-latency
input path macOS offers to user space.

### Benchmarking

Every tap records `perf_counter_ns()` timestamps (monotonic — never
wall-clock): capture time, detection start/end, click dispatch. Use:

```bash
python bot.py --perf-log performance.log
```

The report distinguishes **BOT PROCESSING TIME** (per-tap detection, ~ms)
from **TOTAL GAME TIME** (first click → tenth click, which includes the
game's own render latency between rounds). Nothing is written to disk
during the live loop; the log is flushed after the game ends.

## Project structure

```
iphone-finger-bot/
├── bot.py          # CLI + live loop + test/debug modes + ESC watcher
├── capture.py      # Quartz window capture / mss fallback / synthetic (tests)
├── detector.py     # HSV orange segmentation over 6 ROIs (pure NumPy/OpenCV)
├── clicker.py      # native CGEvent clicks + coordinate helpers + permissions
├── calibrate.py    # interactive calibration wizard
├── config.py       # config load/save/validate
├── benchmark.py    # perf_counter_ns timing, report, performance.log
├── config.json     # calibration output (template until calibrated)
├── requirements.txt
├── tests/
│   ├── test_detector.py          # synthetic fixtures: 6 targets, edge cases
│   ├── test_real_screenshots.py  # regression on real game screenshots
│   ├── test_config.py
│   └── test_coordinates.py       # Retina scaling + window-move math
└── samples/        # real screenshots for offline checks (git-ignored)
```

Run the tests:

```bash
.venv/bin/python -m unittest discover -s tests
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "iPhone Mirroring window not found" | Open iPhone Mirroring first; check `--list-windows` |
| Capture returns black / None | Grant Screen Recording permission, restart Terminal |
| Clicks do nothing | Grant Accessibility permission, restart Terminal |
| Clicks land off-button | Re-run `--calibrate`; check `--verify-coords` |
| ROI boxes overlap / "ROI rectangles overlap" error | Reduce `roi_size` in `config.json` (default 105). No re-calibration needed — centers are stored separately |
| Wrong button detected | Tighten `orange_hsv` in `config.json`; check `--test` |
| Window moved since calibration | Handled automatically; re-calibrate only after resize |
| Bot never taps | Game may not have started — it waits for orange; check `--test` |
| Click doesn't reach iPhone | Click the mirroring window once manually to focus it |

## Emergency stop

**ESC** anywhere, or **Ctrl+C** in the terminal. The bot also stops itself
after the 10th tap.

## License

MIT — see [LICENSE](LICENSE).
