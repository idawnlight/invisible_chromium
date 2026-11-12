# invisible-chromium

Python stealth controller for patched Linux Desktop Chromium (`157.0.8103.50`), combining native Blink/V8/CDP C++ hardware normalization with `invisible_core` seed-deterministic human cursor trajectories and typing cadence.

## Design Highlights

- **Native `--remote-debugging-pipe` Transport**: Communicates over anonymous file descriptors (`fd 3` / `fd 4`) with zero open TCP debugging ports and `navigator.webdriver === false`.
- **Zero `Runtime.enable` Leak**: Evaluates page expressions without enabling the DevTools Runtime domain and runs all internal selector, bounding-box, and actionability checks inside a dedicated `Page.createIsolatedWorld` utility world invisible to page scripts.
- **Linux Desktop Hardware Normalization**: Drives `INVISIBLE_CHROMIUM_PROFILE` (`DesktopHardwareProfile` / `DesktopHardwareProfile.from_seed(seed)`) so server core counts, RAM, V8 `jsHeapSizeLimit`, and software `llvmpipe` WebGL telltales match a standard Intel Mesa OpenGL 4.6 Linux Desktop even inside GPU-less containers.
- **Shared Humanize Engine**: Reuses `invisible_core.juggler` (`CursorMotion`, `PointerPersona`, `Pacer`, `plan_approach`, `plan_idle`, `plan_scroll`, `keylayout`) for Fitts-law approach curves, off-center Gaussian landings, wheel-scroll hand drift, and realistic Shift/dwell/inter-keystroke timing.

## Quickstart

```python
from invisible_chromium import DesktopHardwareProfile, InvisibleChromium

profile = DesktopHardwareProfile.from_seed(42)
with InvisibleChromium(seed=42, headless=True, profile=profile) as browser:
    page = browser.new_page()
    page.goto("https://example.com/")
    print(page.title())
    page.click("a")
```
