"""Real-browser E2E tests for invisible-chromium against patched Chromium 157."""
from __future__ import annotations

import http.server
import math
from pathlib import Path
import socketserver
import struct
import threading
from typing import Iterator

import pytest

from invisible_chromium import DesktopHardwareProfile, InvisibleChromium


@pytest.fixture(scope="module")
def local_server() -> Iterator[str]:
    pages: dict[str, bytes] = {
        "/": b"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Stealth E2E</title></head>
<body style="margin:0; height:3200px; font-family:sans-serif;">
  <button id="btn" style="position:absolute; left:420px; top:260px; width:160px; height:48px;">
    Submit Action
  </button>
  <input id="field" type="text" style="position:absolute; left:120px; top:120px; width:240px; height:32px;" />
  <script>
    window.__events = { moves: [], downs: [], ups: [], clicks: [], keys: [], wheels: [] };
    window.addEventListener('pointermove', (e) => {
      window.__events.moves.push({ x: e.clientX, y: e.clientY, pressure: e.pressure, t: performance.now() });
    });
    window.addEventListener('pointerdown', (e) => {
      window.__events.downs.push({ x: e.clientX, y: e.clientY, pressure: e.pressure, buttons: e.buttons, pointerType: e.pointerType, t: e.timeStamp });
    });
    window.addEventListener('pointerup', (e) => {
      window.__events.ups.push({ x: e.clientX, y: e.clientY, pressure: e.pressure, buttons: e.buttons, t: e.timeStamp });
    });
    document.getElementById('btn').addEventListener('click', (e) => {
      window.__events.clicks.push({ x: e.clientX, y: e.clientY, isTrusted: e.isTrusted });
    });
    document.addEventListener('keydown', (e) => {
      window.__events.keys.push({ type: 'down', key: e.key, code: e.code, keyCode: e.keyCode, shift: e.shiftKey, location: e.location, isTrusted: e.isTrusted });
    });
    document.addEventListener('keyup', (e) => {
      window.__events.keys.push({ type: 'up', key: e.key, code: e.code, keyCode: e.keyCode, shift: e.shiftKey, location: e.location, isTrusted: e.isTrusted });
    });
    window.addEventListener('wheel', (e) => {
      window.__events.wheels.push({ dx: e.deltaX, dy: e.deltaY, x: e.clientX, y: e.clientY });
    }, { passive: true });
  </script>
</body>
</html>""",
        "/trap": b"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Trap Page</title></head>
<body>
  <input id="secret-input" value="initial" />
  <button id="secret-btn" style="width:120px;height:40px;">Go</button>
  <script>
    window.__traps = { querySelectorCalls: 0, stackGetterHits: 0, consoleHits: 0 };
    const origQS = Document.prototype.querySelector;
    Document.prototype.querySelector = function(...args) {
      window.__traps.querySelectorCalls += 1;
      return origQS.apply(this, args);
    };
    const errProbe = new Error('probe');
    Object.defineProperty(errProbe, 'stack', {
      get() {
        window.__traps.stackGetterHits += 1;
        return '';
      }
    });
    console.debug(errProbe);
    window.__initialKeys = null;
    window.__initialKeys = Object.getOwnPropertyNames(window).sort();
  </script>
</body>
</html>""",
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/headers":
                import json

                payload = json.dumps(dict(self.headers)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            body = pages.get(self.path, pages["/"])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            srv.shutdown()


@pytest.mark.browser
def test_pipe_launch_stealth_and_hardware_profile(local_server: str) -> None:
    profile = DesktopHardwareProfile(
        hardware_concurrency=4,
        device_memory_gb=4,
        webgl_vendor="Google Inc. (Intel)",
        webgl_renderer="ANGLE (Intel, Mesa Intel(R) Xe Graphics (TGL GT2), OpenGL 4.6)",
    )
    with InvisibleChromium(
        seed=101,
        headless=True,
        profile=profile,
        timezone_id="Europe/Berlin",
        locale="en-US",
    ) as browser:
        assert "157.0.8103.50" in browser.version["product"]
        page = browser.new_page()
        page.goto(f"{local_server}/")

        data = page.evaluate(
            """() => {
              const c = document.createElement('canvas');
              const gl = c.getContext('webgl2', { failIfMajorPerformanceCaveat: true });
              const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
              return {
                secure: window.isSecureContext,
                webdriver: navigator.webdriver,
                cores: navigator.hardwareConcurrency,
                mem: navigator.deviceMemory,
                heap: performance.memory?.jsHeapSizeLimit,
                tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
                availLess: screen.availHeight < screen.height,
                chromeGap: outerHeight - innerHeight,
                hasGl2WithCaveatFlag: !!gl,
                vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
                renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
                lineWidth: gl ? Array.from(gl.getParameter(gl.ALIASED_LINE_WIDTH_RANGE)) : null,
                maxSamples: gl ? gl.getParameter(gl.MAX_SAMPLES) : null,
              };
            }"""
        )

        assert data["secure"] is True
        assert data["webdriver"] is False
        assert data["cores"] == 4
        assert data["mem"] == 4
        # 4 GiB V8 isolate cap (~2.17 GB quantized) rather than 4 GB host cap!
        assert 1_800_000_000 <= data["heap"] <= 2_500_000_000
        assert data["tz"] == "Europe/Berlin"
        assert data["availLess"] is True
        assert data["chromeGap"] >= 70
        assert data["hasGl2WithCaveatFlag"] is True
        assert data["vendor"] == "Google Inc. (Intel)"
        assert data["renderer"] == "ANGLE (Intel, Mesa Intel(R) Xe Graphics (TGL GT2), OpenGL 4.6)"
        assert data["lineWidth"] == [1.0, 7.375]
        assert data["maxSamples"] == 8


@pytest.mark.browser
def test_isolated_world_zero_main_world_leak(local_server: str) -> None:
    with InvisibleChromium(seed=202, headless=True, max_motion_seconds=0.25) as browser:
        page = browser.new_page()
        page.goto(f"{local_server}/trap")

        assert page.locator("#secret-btn").is_visible() is True
        assert page.locator("#secret-btn").text_content() == "Go"
        box = page.locator("#secret-btn").bounding_box()
        assert box is not None and box["width"] == 120
        page.fill("#secret-input", "clean-value")
        page.click("#secret-btn")

        audit = page.evaluate(
            """() => {
              const currentKeys = Object.getOwnPropertyNames(window).sort();
              const added = currentKeys.filter((k) => !window.__initialKeys.includes(k));
              return {
                traps: window.__traps,
                addedGlobals: added,
                inputValue: document.getElementById('secret-input').value,
              };
            }"""
        )
        assert audit["inputValue"] == "clean-value"
        assert audit["traps"]["querySelectorCalls"] == 0
        assert audit["traps"]["stackGetterHits"] == 0
        assert audit["addedGlobals"] == []


@pytest.mark.browser
def test_humanized_click_curve_pressure_and_typing(local_server: str, tmp_path: Path) -> None:
    with InvisibleChromium(seed=303, headless=True, max_motion_seconds=0.45) as browser:
        page = browser.new_page()
        page.goto(f"{local_server}/")

        page.click("#btn")
        events = page.evaluate("window.__events")

        moves = events["moves"]
        downs = events["downs"]
        ups = events["ups"]
        clicks = events["clicks"]

        assert len(moves) >= 6, f"Expected curved multi-waypoint path, got {len(moves)} moves"
        assert len(clicks) == 1
        assert clicks[0]["isTrusted"] is True
        assert downs[0]["pressure"] == 0.5
        assert downs[0]["pointerType"] == "mouse"
        assert ups[0]["t"] - downs[0]["t"] >= 10.0

        # Verify path is genuinely curved (non-collinear) and lands off exact geometric center (500, 284).
        p0, p1 = moves[0], moves[-1]
        dx, dy = p1["x"] - p0["x"], p1["y"] - p0["y"]
        chord = math.hypot(dx, dy)
        assert chord > 20.0
        max_perp = max(
            abs(dy * m["x"] - dx * m["y"] + p1["x"] * p0["y"] - p1["y"] * p0["x"]) / chord
            for m in moves[1:-1]
        )
        assert max_perp > 0.25
        assert (round(clicks[0]["x"], 2), round(clicks[0]["y"], 2)) != (500.0, 284.0)

        # Verify humanized typing with Shift runs + Unicode fallback.
        page.evaluate("window.__events.keys = []")
        page.type("#field", "Hi! 你好")
        val = page.evaluate("document.getElementById('field').value")
        keys = page.evaluate("window.__events.keys")
        assert val == "Hi! 你好"

        shift_downs = [k for k in keys if k["type"] == "down" and k["code"] == "ShiftLeft"]
        h_downs = [k for k in keys if k["type"] == "down" and k["code"] == "KeyH"]
        i_downs = [k for k in keys if k["type"] == "down" and k["code"] == "KeyI"]
        bang_downs = [k for k in keys if k["type"] == "down" and k["code"] == "Digit1"]

        assert len(shift_downs) == 2
        assert h_downs[0]["key"] == "H" and h_downs[0]["shift"] is True and h_downs[0]["keyCode"] == 72
        assert i_downs[0]["key"] == "i" and i_downs[0]["shift"] is False and i_downs[0]["keyCode"] == 73
        assert bang_downs[0]["key"] == "!" and bang_downs[0]["shift"] is True and bang_downs[0]["keyCode"] == 49

        # Verify humanized wheel scroll + CSS-scaled clip screenshot.
        page.mouse.wheel(0, 500)
        scroll_y = page.evaluate("window.scrollY")
        wheels = page.evaluate("window.__events.wheels")
        assert scroll_y > 250
        assert len(wheels) >= 3

        shot_path = tmp_path / "clip.png"
        png = page.screenshot(
            path=shot_path,
            clip={"x": 10, "y": 10, "width": 200, "height": 100},
            scale="css",
        )
        assert shot_path.is_file()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", png[16:24])
        assert (width, height) == (200, 100)


@pytest.mark.browser
def test_cookies_headers_and_persistent_user_data_dir(local_server: str, tmp_path: Path) -> None:
    profile_dir = tmp_path / "user-profile"

    with InvisibleChromium(seed=404, headless=True, user_data_dir=profile_dir) as browser:
        browser.add_cookies(
            [{"name": "session_tok", "value": "abc123", "url": local_server}]
        )
        page = browser.new_page()
        page.set_extra_http_headers({"X-Custom-Stealth": "verified"})
        page.goto(f"{local_server}/headers")
        hdrs = page.evaluate("JSON.parse(document.body.innerText)")
        assert hdrs.get("X-Custom-Stealth") == "verified"
        assert "session_tok=abc123" in hdrs.get("Cookie", "")
        page.evaluate("localStorage.setItem('persisted_key', 'survived_restart')")

    with InvisibleChromium(seed=404, headless=True, user_data_dir=profile_dir) as browser2:
        page2 = browser2.new_page()
        page2.goto(f"{local_server}/")
        val = page2.evaluate("localStorage.getItem('persisted_key')")
        assert val == "survived_restart"
