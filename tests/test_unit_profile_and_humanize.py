"""Fast unit tests for hardware profile validation, binary seal, and humanized planners."""
from __future__ import annotations

import pytest

from invisible_chromium.binary import load_seal
from invisible_chromium.humanize import KEY_TABLE, HumanController
from invisible_chromium.profile import (
    LINUX_INTEL_MESA_RENDERERS,
    DesktopHardwareProfile,
)


def test_default_hardware_profile_json() -> None:
    p = DesktopHardwareProfile()
    d = p.to_dict()
    assert d == {
        "hardwareConcurrency": 8,
        "deviceMemory": 8,
        "cpuPerformance": 3,
        "normalizeWebgl": True,
        "webglVendor": "Google Inc. (Intel)",
        "webglRenderer": "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)",
    }


def test_hardware_profile_validation() -> None:
    with pytest.raises(ValueError, match="device_memory_gb"):
        DesktopHardwareProfile(device_memory_gb=6)
    with pytest.raises(ValueError, match="hardware_concurrency"):
        DesktopHardwareProfile(hardware_concurrency=0)
    with pytest.raises(ValueError, match="cpu_performance"):
        DesktopHardwareProfile(cpu_performance=5)


def test_hardware_profile_seed_determinism_and_diversity() -> None:
    a1 = DesktopHardwareProfile.from_seed(101)
    a2 = DesktopHardwareProfile.from_seed(101)
    assert a1 == a2

    renderers = {DesktopHardwareProfile.from_seed(s).webgl_renderer for s in range(1, 60)}
    assert len(renderers) >= 3
    valid_renderers = {r for _, r in LINUX_INTEL_MESA_RENDERERS}
    assert renderers.issubset(valid_renderers)


def test_seal_metadata_is_valid() -> None:
    seal = load_seal()
    assert seal["version"] == "158.0.8127.36"
    asset = seal["assets"]["linux-x86_64"]
    assert len(asset["sha256"]) == 64
    assert asset["size_bytes"] > 100_000_000
    assert asset["entry_rel"] == "chrome"
    assert asset["url"].startswith(
        "https://github.com/idawnlight/chromium_antidetect_patch/releases/download/"
    )


def test_binary_downloader_uses_direct_public_release_only() -> None:
    import inspect
    from invisible_chromium import binary

    src = inspect.getsource(binary)
    assert "api.github.com" not in src
    assert "gh auth" not in src
    assert "GITHUB_TOKEN" not in src
    assert "GH_TOKEN" not in src


def test_key_table_mappings() -> None:
    assert KEY_TABLE["a"]["code"] == "KeyA"
    assert KEY_TABLE["a"]["requires_shift"] is False
    assert KEY_TABLE["a"]["windowsVirtualKeyCode"] == 65

    assert KEY_TABLE["A"]["code"] == "KeyA"
    assert KEY_TABLE["A"]["requires_shift"] is True
    assert KEY_TABLE["A"]["windowsVirtualKeyCode"] == 65

    assert KEY_TABLE["!"]["code"] == "Digit1"
    assert KEY_TABLE["!"]["requires_shift"] is True
    assert KEY_TABLE["!"]["windowsVirtualKeyCode"] == 49

    assert KEY_TABLE["Enter"]["windowsVirtualKeyCode"] == 13
    assert KEY_TABLE["Enter"]["text"] == "\r"
    assert KEY_TABLE["Shift"]["code"] == "ShiftLeft"


def test_human_controller_seed_reproducibility() -> None:
    viewport = (1280.0, 720.0)
    box = (400.0, 300.0, 140.0, 44.0)

    moves_1: list[tuple[float, float]] = []
    hc1 = HumanController(seed=777, max_motion_seconds=0.05)
    land1 = hc1.approach_box(box, viewport, lambda x, y: moves_1.append((x, y)))

    moves_2: list[tuple[float, float]] = []
    hc2 = HumanController(seed=777, max_motion_seconds=0.05)
    land2 = hc2.approach_box(box, viewport, lambda x, y: moves_2.append((x, y)))

    hc3 = HumanController(seed=888, max_motion_seconds=0.05)
    land3 = hc3.approach_box(box, viewport, lambda x, y: None)

    assert land1 == land2
    assert land1 != land3
    # Landing is inside box and not on exact center (470.0, 322.0)
    assert 400.0 < land1[0] < 540.0
    assert 300.0 < land1[1] < 344.0
    assert (round(land1[0], 3), round(land1[1], 3)) != (470.0, 322.0)


def test_pipe_cdp_post_and_auth_callback_do_not_deadlock_reader() -> None:
    import json
    import os
    import threading
    from invisible_chromium.browser import InvisibleChromium
    from invisible_chromium.cdp import PipeCDPConnection

    c_r, s_w = os.pipe()
    s_r, c_w = os.pipe()
    conn = PipeCDPConnection(c_r, c_w)
    received: list[dict] = []
    got_two = threading.Event()

    def peer_reader() -> None:
        buf = bytearray()
        while not got_two.is_set():
            chunk = os.read(s_r, 4096)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                idx = buf.find(0)
                if idx < 0:
                    break
                msg = json.loads(bytes(buf[:idx]).decode("utf-8"))
                del buf[: idx + 1]
                received.append(msg)
                if len(received) >= 2:
                    got_two.set()

    t = threading.Thread(target=peer_reader, daemon=True)
    t.start()
    try:
        br = InvisibleChromium.__new__(InvisibleChromium)
        br._cdp = conn
        br._proxy_user = "alice"
        br._proxy_pass = "secret"
        conn.subscribe(br._on_browser_event)

        for ev in (
            {
                "sessionId": "s1",
                "method": "Fetch.requestPaused",
                "params": {"requestId": "r1"},
            },
            {
                "sessionId": "s1",
                "method": "Fetch.authRequired",
                "params": {"requestId": "r2"},
            },
        ):
            os.write(s_w, json.dumps(ev).encode("utf-8") + b"\x00")

        assert got_two.wait(timeout=2.0), f"expected 2 non-blocking Fetch replies, got {received}"
        assert [m["method"] for m in received] == [
            "Fetch.continueRequest",
            "Fetch.continueWithAuth",
        ]
    finally:
        conn.close()
        for fd in (s_w, s_r):
            try:
                os.close(fd)
            except OSError:
                pass


def test_browser_cookies_url_filter_and_page_goto_commit_and_timeout() -> None:
    import pytest
    from invisible_chromium.browser import InvisibleChromium
    from invisible_chromium.page import ChromiumPage

    class FakeCDP:
        def __init__(self, never_load: bool = False) -> None:
            self.never_load = never_load
            self.subs = []

        def subscribe(self, cb):
            self.subs.append(cb)
            return lambda: None

        def send(self, method: str, params=None, *, session_id=None, timeout=30.0):
            if method == "Storage.getCookies":
                return {
                    "cookies": [
                        {"name": "a", "domain": ".alpha.test", "path": "/", "secure": False},
                        {"name": "b", "domain": "beta.test", "path": "/app", "secure": True},
                    ]
                }
            if method in ("Page.enable", "Runtime.enable"):
                return {}
            if method == "Page.setLifecycleEventsEnabled":
                return {}
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "F1"}}}
            if method == "Page.createIsolatedWorld":
                return {"executionContextId": 7}
            if method == "Page.navigate":
                for cb in list(self.subs):
                    cb(session_id, "Page.frameNavigated", {"frame": {"id": "F1"}})
                return {"frameId": "F1"}
            if method == "Runtime.evaluate":
                return {"result": {"value": "loading" if self.never_load else "complete"}}
            return {}

    br = InvisibleChromium.__new__(InvisibleChromium)
    br._cdp = FakeCDP()
    assert [c["name"] for c in br.cookies(["http://sub.alpha.test/home"])] == ["a"]
    assert [c["name"] for c in br.cookies(["http://beta.test/app/x"])] == []
    assert [c["name"] for c in br.cookies(["https://beta.test/app/x"])] == ["b"]

    fast_cdp = FakeCDP(never_load=True)
    page = ChromiumPage(fast_cdp, target_id="T1", session_id="S1", seed=1, page_number=1, humanize=False, max_motion_seconds=0.1)
    # commit succeeds immediately via frameNavigated even while readyState is still "loading"
    assert page.goto("https://alpha.test/", wait_until="commit", timeout=0.4) == {"frameId": "F1"}
    # load raises TimeoutError when neither load event nor readyState=='complete' arrives
    with pytest.raises(TimeoutError):
        page.goto("https://alpha.test/", wait_until="load", timeout=0.25)
