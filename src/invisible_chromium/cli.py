"""CLI helper to install, inspect, and self-test the patched Chromium binary."""
from __future__ import annotations

import argparse
import json
import sys

from . import DesktopHardwareProfile, InvisibleChromium, __version__, ensure_chromium_binary
from .binary import load_seal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="invisible-chromium")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version", help="Print package and pinned Chromium version")
    p_install = sub.add_parser("install", help="Ensure verified Chromium binary is installed")
    p_install.add_argument("--binary-path", default=None)

    p_doctor = sub.add_parser("doctor", help="Launch Chromium and print key desktop/stealth signals")
    p_doctor.add_argument("--seed", type=int, default=42)
    p_doctor.add_argument("--binary-path", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "version":
        seal = load_seal()
        print(
            json.dumps(
                {
                    "invisible_chromium": __version__,
                    "chromium_version": seal.get("version"),
                    "release_tag": seal.get("tag"),
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "install":
        path = ensure_chromium_binary(args.binary_path)
        print(str(path))
        return 0

    if args.cmd == "doctor":
        import http.server
        import socketserver
        import threading

        class _QuietHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b"<!doctype html><title>doctor</title><h1>ok</h1>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

        profile = DesktopHardwareProfile.from_seed(args.seed)
        with socketserver.TCPServer(("127.0.0.1", 0), _QuietHandler) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            try:
                with InvisibleChromium(
                    seed=args.seed,
                    headless=True,
                    binary_path=args.binary_path,
                    profile=profile,
                ) as browser:
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{port}/")
                    report = page.evaluate(
                        """() => {
                          const c = document.createElement('canvas');
                          const gl = c.getContext('webgl2');
                          const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
                          return {
                            userAgent: navigator.userAgent,
                            isSecureContext: window.isSecureContext,
                            webdriver: navigator.webdriver,
                            hardwareConcurrency: navigator.hardwareConcurrency,
                            deviceMemory: navigator.deviceMemory,
                            jsHeapSizeLimit: performance.memory?.jsHeapSizeLimit,
                            screen: [screen.width, screen.height, screen.availWidth, screen.availHeight],
                            chromeGapHeight: outerHeight - innerHeight,
                            webglVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
                            webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
                            lineWidthRange: gl ? Array.from(gl.getParameter(gl.ALIASED_LINE_WIDTH_RANGE)) : null,
                          };
                        }"""
                    )
                    print(json.dumps(report, indent=2))
            finally:
                httpd.shutdown()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
