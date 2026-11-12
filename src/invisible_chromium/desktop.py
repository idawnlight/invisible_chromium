"""Managed virtual Linux X11 desktop (Xvfb + Openbox workarea margins)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


class ManagedDesktop:
    """Starts an isolated Xvfb + Openbox desktop when headless=True on Linux.

    When running inside the `chromium-linux-desktop` container where DISPLAY and
    Openbox are already active (`PROBE_DISPLAY` set or `reuse_existing=True`),
    it reuses that display without spawning nested X servers.
    """

    def __init__(
        self,
        *,
        width: int = 1920,
        height: int = 1080,
        panel_height: int = 36,
        reuse_existing: bool | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.panel_height = panel_height
        self._reuse = (
            bool(os.environ.get("PROBE_DISPLAY"))
            if reuse_existing is None
            else reuse_existing
        )
        self.display: str = ""
        self._tmp_dir: Path | None = None
        self._xvfb_proc: subprocess.Popen[bytes] | None = None
        self._wm_proc: subprocess.Popen[bytes] | None = None

    def start(self) -> str:
        if self._reuse and os.environ.get("DISPLAY"):
            self.display = os.environ["DISPLAY"]
            return self.display

        xvfb_bin = shutil.which("Xvfb")
        if not xvfb_bin:
            raise RuntimeError(
                "Xvfb is required for headless=True on Linux (sudo apt-get install xvfb openbox)"
            )

        self._tmp_dir = Path(tempfile.mkdtemp(prefix="inv-chr-desktop-"))
        r_fd, w_fd = os.pipe()
        try:
            self._xvfb_proc = subprocess.Popen(
                [
                    xvfb_bin,
                    "-displayfd",
                    str(w_fd),
                    "-screen",
                    "0",
                    f"{self.width}x{self.height}x24",
                    "-dpi",
                    "96",
                    "-nolisten",
                    "tcp",
                    "-noreset",
                ],
                pass_fds=(w_fd,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(w_fd)

        buf = b""
        deadline = time.monotonic() + 10.0
        try:
            os.set_blocking(r_fd, False)
            while time.monotonic() < deadline:
                if self._xvfb_proc.poll() is not None:
                    raise RuntimeError("Xvfb exited unexpectedly during startup")
                try:
                    chunk = os.read(r_fd, 64)
                    if chunk:
                        buf += chunk
                        if b"\n" in buf:
                            break
                except BlockingIOError:
                    pass
                time.sleep(0.03)
        finally:
            os.close(r_fd)

        disp_num = buf.decode("ascii", errors="ignore").strip()
        if not disp_num:
            self.close()
            raise RuntimeError("Timed out waiting for Xvfb -displayfd")
        self.display = f":{disp_num}"

        env = {**os.environ, "DISPLAY": self.display}
        xrdb_bin = shutil.which("xrdb")
        if xrdb_bin:
            subprocess.run(
                [xrdb_bin, "-merge"],
                input=b"Xft.dpi: 96\nXft.antialias: 1\nXft.hinting: 1\nXft.hintstyle: hintslight\nXft.rgba: rgb\n",
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

        openbox_bin = shutil.which("openbox")
        if openbox_bin:
            rc_path = self._tmp_dir / "openbox-rc.xml"
            rc_path.write_text(
                f"""<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <margins>
    <top>0</top>
    <bottom>{self.panel_height}</bottom>
    <left>0</left>
    <right>0</right>
  </margins>
</openbox_config>
""",
                encoding="utf-8",
            )
            self._wm_proc = subprocess.Popen(
                [openbox_bin, "--sm-disable", "--config-file", str(rc_path)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            xprop_bin = shutil.which("xprop")
            if xprop_bin:
                for _ in range(30):
                    res = subprocess.run(
                        [xprop_bin, "-root", "-display", self.display, "_NET_WORKAREA"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if "_NET_WORKAREA(" in res.stdout:
                        break
                    time.sleep(0.05)
            else:
                time.sleep(0.15)

        return self.display

    def close(self) -> None:
        for proc in (self._wm_proc, self._xvfb_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
        self._wm_proc = None
        self._xvfb_proc = None
        if self._tmp_dir is not None:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None
