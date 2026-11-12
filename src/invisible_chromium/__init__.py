"""Stealth Linux Desktop Chromium controller powered by native C++ patches and invisible_core."""
from ._version import __version__
from .binary import ensure_chromium_binary, load_seal
from .browser import InvisibleChromium
from .cdp import CDPError, PipeCDPConnection
from .page import ChromiumPage, ElementTimeoutError, Locator, PageEvaluationError
from .profile import DesktopHardwareProfile

__all__ = [
    "__version__",
    "CDPError",
    "ChromiumPage",
    "DesktopHardwareProfile",
    "ElementTimeoutError",
    "InvisibleChromium",
    "Locator",
    "PageEvaluationError",
    "PipeCDPConnection",
    "ensure_chromium_binary",
    "load_seal",
]
