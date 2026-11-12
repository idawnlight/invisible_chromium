"""Session lifecycle and InvisibleChromium browser manager."""
from __future__ import annotations

import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlparse

from invisible_core import resolve_session_timezone

from .binary import ensure_chromium_binary
from .cdp import PipeCDPConnection
from .desktop import ManagedDesktop
from .page import ChromiumPage
from .profile import DesktopHardwareProfile


def _parse_proxy(proxy: str | dict[str, str] | None) -> tuple[str | None, str | None, str | None]:
    if not proxy:
        return None, None, None
    if isinstance(proxy, dict):
        server = proxy.get("server")
        username = proxy.get("username")
        password = proxy.get("password")
        if server and "://" in server and (username is None and "@" in server):
            parsed = urlparse(server)
            username = parsed.username
            password = parsed.password
            scheme = parsed.scheme
            host_port = f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or "")
            server = f"{scheme}://{host_port}"
        return server, username, password

    parsed = urlparse(proxy)
    if parsed.username is not None:
        scheme = parsed.scheme or "http"
        host_port = f"{parsed.hostname}:{parsed.port}" if parsed.port else (parsed.hostname or "")
        return f"{scheme}://{host_port}", parsed.username, parsed.password or ""
    return proxy, None, None


class InvisibleChromium:
    """Stealth Linux Desktop Chromium session driven over --remote-debugging-pipe.

    Example::

        from invisible_chromium import InvisibleChromium

        with InvisibleChromium(seed=42, headless=True) as browser:
            page = browser.new_page()
            page.goto("https://example.com/")
            print(page.title())
    """

    def __init__(
        self,
        *,
        seed: int | None = None,
        headless: bool = True,
        binary_path: str | os.PathLike[str] | None = None,
        profile: DesktopHardwareProfile | dict[str, Any] | None = None,
        locale: str = "en-US",
        accept_languages: str | None = None,
        timezone_id: str | None = None,
        window_size: tuple[int, int] = (1280, 800),
        user_data_dir: str | os.PathLike[str] | None = None,
        proxy: str | dict[str, str] | None = None,
        humanize: bool = True,
        max_motion_seconds: float = 1.2,
        no_sandbox: bool | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.seed = int(seed) if seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        self.headless = bool(headless)
        self._binary_arg = binary_path
        if profile is None:
            self.hardware_profile = DesktopHardwareProfile()
        elif isinstance(profile, dict):
            self.hardware_profile = DesktopHardwareProfile(
                hardware_concurrency=profile.get("hardwareConcurrency", profile.get("hardware_concurrency", 8)),
                device_memory_gb=profile.get("deviceMemory", profile.get("device_memory_gb", 8)),
                cpu_performance=profile.get("cpuPerformance", profile.get("cpu_performance")),
                normalize_webgl=profile.get("normalizeWebgl", profile.get("normalize_webgl", True)),
                webgl_vendor=profile.get("webglVendor", profile.get("webgl_vendor", "Google Inc. (Intel)")),
                webgl_renderer=profile.get(
                    "webglRenderer",
                    profile.get(
                        "webgl_renderer",
                        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)",
                    ),
                ),
            )
        else:
            self.hardware_profile = profile

        self.locale = locale
        self.accept_languages = accept_languages or (
            f"{locale},{locale.split('-')[0]}" if "-" in locale else locale
        )
        self.timezone_id = timezone_id
        self.window_size = window_size
        self._persistent_dir = (
            Path(user_data_dir).expanduser().resolve() if user_data_dir else None
        )
        self._proxy_server, self._proxy_user, self._proxy_pass = _parse_proxy(proxy)
        self.humanize = bool(humanize)
        self.max_motion_seconds = float(max_motion_seconds)
        self._no_sandbox = (
            self._detect_needs_no_sandbox() if no_sandbox is None else bool(no_sandbox)
        )
        self.extra_args = list(extra_args or ())

        self.binary: Path | None = None
        self._desktop: ManagedDesktop | None = None
        self._temp_profile_dir: Path | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._cdp: PipeCDPConnection | None = None
        self._pages: list[ChromiumPage] = []
        self._page_counter = 0
        self._initial_tab_claimed = False
        self._closed = False

    @staticmethod
    def _detect_needs_no_sandbox() -> bool:
        if os.environ.get("INVISIBLE_CHROMIUM_NO_SANDBOX") == "1" or os.environ.get("PROBE_NO_SANDBOX") == "1":
            return True
        if os.geteuid() == 0:
            return True
        try:
            val = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip()
            if val == "1":
                return True
        except OSError:
            pass
        return False

    def __enter__(self) -> "InvisibleChromium":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> "InvisibleChromium":
        if self._cdp is not None:
            return self

        self.binary = ensure_chromium_binary(self._binary_arg)

        env = os.environ.copy()
        env["INVISIBLE_CHROMIUM_PROFILE"] = self.hardware_profile.to_json()
        if self.timezone_id:
            proxy_cfg = None
            if self._proxy_server:
                proxy_cfg = {"server": self._proxy_server}
                if self._proxy_user is not None:
                    proxy_cfg["username"] = self._proxy_user
                if self._proxy_pass is not None:
                    proxy_cfg["password"] = self._proxy_pass
            self.resolved_timezone = resolve_session_timezone(self.timezone_id, proxy_cfg)
            env["TZ"] = self.resolved_timezone
        else:
            self.resolved_timezone = env.get("TZ")

        if self.headless:
            self._desktop = ManagedDesktop()
            env["DISPLAY"] = self._desktop.start()
        elif not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
            raise RuntimeError("headless=False requires DISPLAY or WAYLAND_DISPLAY in environment")

        if not Path("/dev/dri/renderD128").exists() and "LIBGL_ALWAYS_SOFTWARE" not in env:
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"

        if self._persistent_dir is not None:
            profile_dir = self._persistent_dir
            profile_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._temp_profile_dir = Path(tempfile.mkdtemp(prefix="inv-chr-profile-"))
            profile_dir = self._temp_profile_dir

        default_sub = profile_dir / "Default"
        default_sub.mkdir(parents=True, exist_ok=True)
        prefs_file = default_sub / "Preferences"
        prefs_data: dict[str, Any] = {}
        if prefs_file.is_file():
            try:
                prefs_data = json.loads(prefs_file.read_text(encoding="utf-8"))
            except Exception:
                prefs_data = {}
        prefs_data.setdefault("intl", {})["accept_languages"] = self.accept_languages
        prefs_data.setdefault("credentials_enable_service", False)
        prefs_data.setdefault("profile", {})["password_manager_enabled"] = False
        prefs_file.write_text(json.dumps(prefs_data), encoding="utf-8")

        w, h = self.window_size
        args = [
            str(self.binary),
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--remote-debugging-pipe",
            f"--window-size={w},{h}",
            "--ozone-platform=x11",
            "--use-gl=angle",
            "--use-angle=gl",
            f"--lang={self.locale}",
        ]
        if self._no_sandbox:
            args.append("--no-sandbox")
        if self._proxy_server:
            args.append(f"--proxy-server={self._proxy_server}")
        args.extend(self.extra_args)
        args.append("about:blank")

        # Parent writes commands -> child reads FD 3; child writes events -> parent reads FD 4.
        c_read_fd, p_write_fd = os.pipe()
        p_read_fd, c_write_fd = os.pipe()

        def _setup_child_fds() -> None:
            r_fd, w_fd = c_read_fd, c_write_fd
            if w_fd == 3:
                w_fd = os.dup(w_fd)
            if r_fd != 3:
                os.dup2(r_fd, 3)
            if w_fd != 4:
                os.dup2(w_fd, 4)
            for extra in {c_read_fd, c_write_fd, w_fd} - {3, 4}:
                try:
                    os.close(extra)
                except OSError:
                    pass
            os.set_inheritable(3, True)
            os.set_inheritable(4, True)

        try:
            self._proc = subprocess.Popen(
                args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                pass_fds=(c_read_fd, c_write_fd, 3, 4),
                preexec_fn=_setup_child_fds,
            )
        finally:
            os.close(c_read_fd)
            os.close(c_write_fd)

        self._cdp = PipeCDPConnection(read_fd=p_read_fd, write_fd=p_write_fd)
        if self._proxy_user is not None:
            self._cdp.subscribe(self._on_browser_event)

        # Confirm browser pipe readiness cleanly.
        try:
            self._version_info = self._cdp.send("Browser.getVersion", timeout=20.0)
        except Exception as exc:
            stderr_tail = ""
            if self._proc is not None and self._proc.stderr is not None:
                try:
                    if self._proc.poll() is not None:
                        stderr_tail = self._proc.stderr.read().decode("utf-8", errors="replace")[-1500:]
                except Exception:
                    pass
            self.close()
            if stderr_tail:
                raise RuntimeError(f"{exc}\nChromium stderr:\n{stderr_tail}") from exc
            raise
        return self

    def _on_browser_event(
        self, session_id: str | None, method: str, params: dict[str, Any]
    ) -> None:
        if method == "Fetch.authRequired" and self._cdp is not None:
            req_id = params.get("requestId")
            if req_id:
                try:
                    self._cdp.send(
                        "Fetch.continueWithAuth",
                        {
                            "requestId": req_id,
                            "authChallengeResponse": {
                                "response": "ProvideCredentials",
                                "username": self._proxy_user or "",
                                "password": self._proxy_pass or "",
                            },
                        },
                        session_id=session_id,
                    )
                except Exception:
                    pass
        elif method == "Fetch.requestPaused" and self._cdp is not None:
            req_id = params.get("requestId")
            if req_id:
                try:
                    self._cdp.send(
                        "Fetch.continueRequest",
                        {"requestId": req_id},
                        session_id=session_id,
                    )
                except Exception:
                    pass

    @property
    def version(self) -> dict[str, Any]:
        if self._cdp is None:
            raise RuntimeError("Browser not started")
        return dict(self._version_info)

    def new_page(self) -> ChromiumPage:
        if self._cdp is None:
            self.start()
        assert self._cdp is not None

        target_id: str | None = None
        if not self._initial_tab_claimed:
            targets = self._cdp.send("Target.getTargets").get("targetInfos", [])
            for t in targets:
                if t.get("type") == "page" and t.get("url") in ("about:blank", ""):
                    target_id = t["targetId"]
                    self._initial_tab_claimed = True
                    break

        if target_id is None:
            created = self._cdp.send("Target.createTarget", {"url": "about:blank"})
            target_id = created["targetId"]

        attached = self._cdp.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        session_id = attached["sessionId"]
        self._cdp.send("Target.activateTarget", {"targetId": target_id})

        if self._proxy_user is not None:
            self._cdp.send(
                "Fetch.enable",
                {"handleAuthRequests": True},
                session_id=session_id,
            )

        self._page_counter += 1
        page = ChromiumPage(
            self._cdp,
            target_id=target_id,
            session_id=session_id,
            seed=self.seed,
            page_number=self._page_counter,
            humanize=self.humanize,
            max_motion_seconds=self.max_motion_seconds,
        )
        self._pages.append(page)
        return page

    def cookies(self, urls: list[str] | None = None) -> list[dict[str, Any]]:
        if self._cdp is None:
            raise RuntimeError("Browser not started")
        params: dict[str, Any] = {}
        if urls:
            params["urls"] = list(urls)
        return list(self._cdp.send("Storage.getCookies", params).get("cookies", []))

    def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        if self._cdp is None:
            raise RuntimeError("Browser not started")
        self._cdp.send("Storage.setCookies", {"cookies": list(cookies)})

    def clear_cookies(self) -> None:
        if self._cdp is None:
            raise RuntimeError("Browser not started")
        self._cdp.send("Storage.clearCookies")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        browser_close_sent = False
        if self._cdp is not None:
            try:
                self._cdp.send("Browser.close", timeout=3.0)
                browser_close_sent = True
            except Exception:
                pass
            self._cdp.close()
            self._cdp = None

        if self._proc is not None:
            if browser_close_sent and self._proc.poll() is None:
                try:
                    self._proc.wait(timeout=4.0)
                except subprocess.TimeoutExpired:
                    pass
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=3.0)
            if self._proc.stderr is not None:
                try:
                    self._proc.stderr.close()
                except Exception:
                    pass
            self._proc = None

        if self._desktop is not None:
            self._desktop.close()
            self._desktop = None

        if self._temp_profile_dir is not None:
            shutil.rmtree(self._temp_profile_dir, ignore_errors=True)
            self._temp_profile_dir = None
