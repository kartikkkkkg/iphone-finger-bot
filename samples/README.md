# samples/

Reference game screenshots live here on the dev machine (git-ignored):

- `game-orange-4a.png` / `game-orange-4b.png` - live round, button 4 orange
- `game-all-purple.png` - STARTING SOON screen, all purple (no target)

They are used by `tests/test_real_screenshots.py` as a regression test for
the detector (skipped automatically when absent). They are NOT committed to
the repository (`.gitignore`).

A quick offline check with any new screenshot:

```bash
python - <<'EOF'
import cv2, json
import detector
cfg = json.load(open("config.json"))
frame = cv2.imread("samples/game-orange-4a.png")
# crop to the calibrated game region if the screenshot is full-screen:
# frame = frame[y:y+h, x:x+w]
target, scores, info = detector.detect(frame, cfg)
print("target:", target, "scores:", [f"{s:.2f}" for s in scores])
EOF
```
