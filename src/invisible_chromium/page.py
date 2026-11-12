"""Page, Mouse, Keyboard, and Locator abstractions over stealth CDP."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import threading
import time
from typing import Any

from .cdp import CDPError, PipeCDPConnection
from .humanize import CDP_BUTTON_MASK, HumanController

UTILITY_WORLD_NAME = "__ic_utility__"


class PageEvaluationError(RuntimeError):
    """Raised when evaluated JavaScript throws an uncaught exception."""


class ElementTimeoutError(TimeoutError):
    """Raised when an element does not reach the required actionable state in time."""


class Mouse:
    """Direct mouse control bound to a ChromiumPage's humanized trajectory engine."""

    def __init__(self, page: "ChromiumPage") -> None:
        self._page = page

    def move(self, x: float, y: float) -> None:
        vp = self._page._viewport_size()
        self._page._human.move_to_point(x, y, vp, self._page._emit_mouse_move)

    def click(
        self, x: float, y: float, *, button: str = "left", click_count: int = 1
    ) -> None:
        self.move(x, y)
        self._page._human.perform_click(
            x,
            y,
            button=button,
            click_count=click_count,
            dispatch_mouse=self._page._dispatch_mouse,
        )

    def wheel(self, delta_x: float, delta_y: float) -> None:
        vp = self._page._viewport_size()
        self._page._human.scroll_wheel(
            delta_x, delta_y, vp, self._page._emit_mouse_move, self._page._emit_wheel
        )


class KeyboardFacade:
    """Direct keyboard control bound to a ChromiumPage's humanized typing engine."""

    def __init__(self, page: "ChromiumPage") -> None:
        self._page = page

    def type(self, text: str) -> None:
        self._page._human.type_text(
            text, self._page._dispatch_key, self._page._insert_text
        )

    def press(self, key: str) -> None:
        self._page._human.press_combo(key, self._page._dispatch_key)


class Locator:
    """Playwright-style lazy selector locator executing DOM probes in an Isolated World."""

    def __init__(self, page: "ChromiumPage", selector: str) -> None:
        self._page = page
        self.selector = selector

    def click(self, *, button: str = "left", timeout: float = 15.0) -> None:
        self._page.click(self.selector, button=button, timeout=timeout)

    def dblclick(self, *, button: str = "left", timeout: float = 15.0) -> None:
        self._page.dblclick(self.selector, button=button, timeout=timeout)

    def hover(self, *, timeout: float = 15.0) -> None:
        self._page.hover(self.selector, timeout=timeout)

    def fill(self, value: str, *, timeout: float = 15.0) -> None:
        self._page.fill(self.selector, value, timeout=timeout)

    def type(self, text: str, *, timeout: float = 15.0) -> None:
        self._page.type(self.selector, text, timeout=timeout)

    def press(self, key: str, *, timeout: float = 15.0) -> None:
        self._page.press(self.selector, key, timeout=timeout)

    def wait_for(self, *, state: str = "visible", timeout: float = 15.0) -> None:
        self._page.wait_for_selector(self.selector, state=state, timeout=timeout)

    def count(self) -> int:
        return int(
            self._page.evaluate_isolated(
                f"document.querySelectorAll({json.dumps(self.selector)}).length"
            )
        )

    def is_visible(self) -> bool:
        res = self._page._probe_selector(self.selector)
        return bool(res.get("found") and res.get("visible"))

    def text_content(self, *, timeout: float = 15.0) -> str | None:
        self._page.wait_for_selector(self.selector, state="attached", timeout=timeout)
        return self._page.evaluate_isolated(
            f"document.querySelector({json.dumps(self.selector)})?.textContent ?? null"
        )

    def inner_text(self, *, timeout: float = 15.0) -> str:
        self._page.wait_for_selector(self.selector, state="visible", timeout=timeout)
        val = self._page.evaluate_isolated(
            f"document.querySelector({json.dumps(self.selector)})?.innerText ?? ''"
        )
        return str(val)

    def get_attribute(self, name: str, *, timeout: float = 15.0) -> str | None:
        self._page.wait_for_selector(self.selector, state="attached", timeout=timeout)
        return self._page.evaluate_isolated(
            f"document.querySelector({json.dumps(self.selector)})?.getAttribute({json.dumps(name)}) ?? null"
        )

    def bounding_box(self, *, timeout: float = 15.0) -> dict[str, float] | None:
        self._page.wait_for_selector(self.selector, state="visible", timeout=timeout)
        info = self._page._probe_selector(self.selector)
        if not info.get("visible"):
            return None
        b = info["box"]
        return {"x": b[0], "y": b[1], "width": b[2], "height": b[3]}


class ChromiumPage:
    """Single browser tab controlled via flattened CDP session without Runtime.enable."""

    def __init__(
        self,
        cdp: PipeCDPConnection,
        *,
        target_id: str,
        session_id: str,
        seed: int,
        page_number: int,
        humanize: bool = True,
        max_motion_seconds: float = 1.2,
    ) -> None:
        self._cdp = cdp
        self.target_id = target_id
        self.session_id = session_id
        self._lock = threading.Lock()
        self._main_frame_id: str = ""
        self._utility_context_id: int | None = None
        self._lifecycle_events: dict[str, set[str]] = {}
        self._lifecycle_cond = threading.Condition()
        self._closed = False
        self._human = HumanController(
            seed=seed,
            page_number=page_number,
            enabled=humanize,
            max_motion_seconds=max_motion_seconds,
        )
        self.mouse = Mouse(self)
        self.keyboard = KeyboardFacade(self)

        self._unsub = self._cdp.subscribe(self._on_cdp_event)
        self._cdp.send("Page.enable", session_id=self.session_id)
        self._cdp.send(
            "Page.setLifecycleEventsEnabled",
            {"enabled": True},
            session_id=self.session_id,
        )
        tree = self._cdp.send("Page.getFrameTree", session_id=self.session_id)
        self._main_frame_id = tree["frameTree"]["frame"]["id"]

    def _on_cdp_event(
        self, session_id: str | None, method: str, params: dict[str, Any]
    ) -> None:
        if session_id != self.session_id:
            return
        if method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if not frame.get("parentId"):
                with self._lifecycle_cond:
                    self._main_frame_id = frame.get("id", self._main_frame_id)
                    self._utility_context_id = None
                    self._lifecycle_cond.notify_all()
        elif method == "Page.lifecycleEvent":
            fid = params.get("frameId", "")
            name = params.get("name", "")
            with self._lifecycle_cond:
                if name == "init":
                    self._lifecycle_events[fid] = {"init"}
                else:
                    self._lifecycle_events.setdefault(fid, set()).add(name)
                self._lifecycle_cond.notify_all()

    @property
    def url(self) -> str:
        return str(self.evaluate_isolated("window.location.href"))

    def title(self) -> str:
        return str(self.evaluate_isolated("document.title"))

    def content(self) -> str:
        return str(self.evaluate_isolated("document.documentElement.outerHTML"))

    def locator(self, selector: str) -> Locator:
        return Locator(self, selector)

    def bring_to_front(self) -> None:
        self._cdp.send("Target.activateTarget", {"targetId": self.target_id})

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self._cdp.send("Network.enable", session_id=self.session_id)
        self._cdp.send(
            "Network.setExtraHTTPHeaders",
            {"headers": dict(headers)},
            session_id=self.session_id,
        )

    def goto(
        self,
        url: str,
        *,
        wait_until: str = "load",
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        target_event = {
            "commit": "commit",
            "domcontentloaded": "DOMContentLoaded",
            "load": "load",
            "networkidle": "networkIdle",
        }.get(wait_until.lower())
        if not target_event:
            raise ValueError(
                f"Unsupported wait_until={wait_until!r}; choose commit, domcontentloaded, load, or networkidle"
            )

        with self._lifecycle_cond:
            self._utility_context_id = None
            self._lifecycle_events.clear()

        nav = self._cdp.send(
            "Page.navigate", {"url": url}, session_id=self.session_id, timeout=timeout
        )
        if nav.get("errorText"):
            raise RuntimeError(f"Navigation to {url} failed: {nav['errorText']}")

        frame_id = nav.get("frameId") or self._main_frame_id
        deadline = time.monotonic() + timeout
        with self._lifecycle_cond:
            while True:
                seen = self._lifecycle_events.get(frame_id, set())
                if target_event in seen:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lifecycle_cond.wait(timeout=min(remaining, 0.25))

        # Confirm document readiness in the Isolated World as a fallback for cached/instant pages.
        ready_Target = (
            ("interactive", "complete")
            if wait_until.lower() in ("commit", "domcontentloaded")
            else ("complete",)
        )
        while time.monotonic() < deadline:
            try:
                state = self.evaluate_isolated("document.readyState")
                if state in ready_Target:
                    break
            except Exception:
                pass
            time.sleep(0.05)

        return nav

    def reload(self, *, wait_until: str = "load", timeout: float = 30.0) -> None:
        with self._lifecycle_cond:
            self._utility_context_id = None
            self._lifecycle_events.clear()
        self._cdp.send("Page.reload", session_id=self.session_id, timeout=timeout)
        self.wait_for_load_state(wait_until, timeout=timeout)

    def wait_for_load_state(self, state: str = "load", *, timeout: float = 30.0) -> None:
        target_event = {
            "domcontentloaded": "DOMContentLoaded",
            "load": "load",
            "networkidle": "networkIdle",
        }.get(state.lower(), "load")
        deadline = time.monotonic() + timeout
        with self._lifecycle_cond:
            while time.monotonic() < deadline:
                if target_event in self._lifecycle_events.get(self._main_frame_id, set()):
                    return
                if target_event in ("DOMContentLoaded", "load"):
                    try:
                        rs = self.evaluate_isolated("document.readyState")
                        if rs == "complete" or (
                            target_event == "DOMContentLoaded" and rs == "interactive"
                        ):
                            return
                    except Exception:
                        pass
                self._lifecycle_cond.wait(timeout=0.1)
        raise TimeoutError(f"Timed out waiting for load state {state!r}")

    def _ensure_utility_world(self) -> int:
        if self._utility_context_id is not None:
            return self._utility_context_id
        tree = self._cdp.send("Page.getFrameTree", session_id=self.session_id)
        self._main_frame_id = tree["frameTree"]["frame"]["id"]
        res = self._cdp.send(
            "Page.createIsolatedWorld",
            {
                "frameId": self._main_frame_id,
                "worldName": UTILITY_WORLD_NAME,
                "grantUniveralAccess": True,
            },
            session_id=self.session_id,
        )
        ctx_id = int(res["executionContextId"])
        self._utility_context_id = ctx_id
        return ctx_id

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Evaluate JavaScript in the page's MAIN world without calling Runtime.enable."""
        expr = self._wrap_expression(expression, arg, pass_arg=(arg is not None))
        res = self._cdp.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "awaitPromise": True,
                "returnByValue": True,
            },
            session_id=self.session_id,
        )
        return self._unwrap_eval_result(res)

    def evaluate_isolated(self, expression: str, arg: Any = None) -> Any:
        """Evaluate JavaScript in an Isolated World invisible to page scripts."""
        expr = self._wrap_expression(expression, arg, pass_arg=(arg is not None))
        for attempt in range(3):
            ctx_id = self._ensure_utility_world()
            try:
                res = self._cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": expr,
                        "contextId": ctx_id,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                    session_id=self.session_id,
                )
                return self._unwrap_eval_result(res)
            except CDPError as exc:
                self._utility_context_id = None
                if attempt == 2:
                    raise exc
                time.sleep(0.04)
        raise RuntimeError("Failed to evaluate in isolated world")

    @staticmethod
    def _wrap_expression(expression: str, arg: Any, *, pass_arg: bool) -> str:
        stripped = expression.strip()
        if pass_arg:
            serialized = json.dumps(arg)
            return f"({stripped})({serialized})"
        # Support Playwright-style arrow/function expressions such as "() => document.title"
        if stripped.startswith(("() =>", "()=>", "async () =>", "async()=>", "function(", "async function(")):
            return f"({stripped})()"
        return stripped

    @staticmethod
    def _unwrap_eval_result(res: dict[str, Any]) -> Any:
        if "exceptionDetails" in res:
            details = res["exceptionDetails"]
            exc_obj = details.get("exception") or {}
            desc = exc_obj.get("description") or details.get("text") or "JS evaluation error"
            raise PageEvaluationError(desc)
        remote = res.get("result") or {}
        if remote.get("subtype") == "null":
            return None
        if remote.get("type") == "undefined":
            return None
        return remote.get("value")

    def _viewport_size(self) -> tuple[float, float]:
        try:
            dims = self.evaluate_isolated(
                "({w: window.innerWidth, h: window.innerHeight})"
            )
            if isinstance(dims, dict) and dims.get("w") and dims.get("h"):
                return (float(dims["w"]), float(dims["h"]))
        except Exception:
            pass
        return (1272.0, 709.0)

    def _probe_selector(
        self, selector: str, *, scroll_into_view: bool = False
    ) -> dict[str, Any]:
        probe_fn = """(arg) => {
          const el = document.querySelector(arg.selector);
          if (!el) return { found: false, visible: false, reason: 'selector matched no element' };
          if (arg.scroll) {
            el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
          }
          const style = window.getComputedStyle(el);
          if (style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity) === 0) {
            return { found: true, visible: false, reason: 'element hidden by CSS' };
          }
          const rect = el.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) {
            return { found: true, visible: false, reason: 'element has zero bounding box' };
          }
          const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true';
          const cx = rect.left + rect.width / 2;
          const cy = rect.top + rect.height / 2;
          const hit = document.elementFromPoint(cx, cy);
          const unobstructed = !hit || el === hit || el.contains(hit) || hit.contains(el);
          return {
            found: true,
            visible: true,
            enabled: !disabled,
            unobstructed,
            box: [rect.left, rect.top, rect.width, rect.height],
            tag: el.tagName,
          };
        }"""
        res = self.evaluate_isolated(
            probe_fn, {"selector": selector, "scroll": scroll_into_view}
        )
        return res if isinstance(res, dict) else {"found": False, "visible": False}

    def wait_for_selector(
        self, selector: str, *, state: str = "visible", timeout: float = 15.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_reason = "timeout"
        while time.monotonic() < deadline:
            try:
                info = self._probe_selector(selector)
                if state == "attached" and info.get("found"):
                    return info
                if state == "visible" and info.get("found") and info.get("visible"):
                    return info
                if state == "hidden" and (not info.get("found") or not info.get("visible")):
                    return info
                last_reason = str(info.get("reason") or f"state is not {state}")
            except Exception as exc:
                last_reason = str(exc)
            time.sleep(0.05)
        raise ElementTimeoutError(
            f"wait_for_selector({selector!r}, state={state!r}) timed out after {timeout:.1f}s: {last_reason}"
        )

    def _wait_actionable_box(
        self, selector: str, *, timeout: float = 15.0
    ) -> tuple[float, float, float, float]:
        deadline = time.monotonic() + timeout
        last_reason = "not found"
        prev_box: list[float] | None = None
        while time.monotonic() < deadline:
            try:
                info = self._probe_selector(selector, scroll_into_view=True)
                if not info.get("found") or not info.get("visible"):
                    last_reason = str(info.get("reason") or "not visible")
                    prev_box = None
                elif not info.get("enabled", True):
                    last_reason = "element is disabled"
                    prev_box = None
                else:
                    box = [float(v) for v in info["box"]]
                    if prev_box is not None and all(
                        abs(a - b) < 0.5 for a, b in zip(box, prev_box)
                    ):
                        if info.get("unobstructed", True):
                            return (box[0], box[1], box[2], box[3])
                        last_reason = "element center is obscured by another element"
                    else:
                        prev_box = box
                        last_reason = "waiting for layout stability"
            except Exception as exc:
                last_reason = str(exc)
                prev_box = None
            time.sleep(0.035)
        raise ElementTimeoutError(
            f"Element {selector!r} not actionable within {timeout:.1f}s: {last_reason}"
        )

    def click(
        self,
        selector: str,
        *,
        button: str = "left",
        click_count: int = 1,
        timeout: float = 15.0,
    ) -> None:
        box = self._wait_actionable_box(selector, timeout=timeout)
        vp = self._viewport_size()
        lx, ly = self._human.approach_box(box, vp, self._emit_mouse_move)
        self._human.perform_click(
            lx,
            ly,
            button=button,
            click_count=click_count,
            dispatch_mouse=self._dispatch_mouse,
        )

    def dblclick(
        self, selector: str, *, button: str = "left", timeout: float = 15.0
    ) -> None:
        self.click(selector, button=button, click_count=2, timeout=timeout)

    def hover(self, selector: str, *, timeout: float = 15.0) -> None:
        box = self._wait_actionable_box(selector, timeout=timeout)
        vp = self._viewport_size()
        self._human.approach_box(box, vp, self._emit_mouse_move)

    def fill(
        self, selector: str, value: str, *, timeout: float = 15.0
    ) -> None:
        self.click(selector, timeout=timeout)
        # Clear & set value via native prototype setter inside Isolated World + fire trusted-like input/change,
        # or select-all + backspace + type when short. Using select-all + Backspace + insertText/type
        # produces genuine browser editing events!
        self._human.press_combo("Control+a", self._dispatch_key)
        self._human.press_combo("Backspace", self._dispatch_key)
        if value:
            self._insert_text(value)
        self.evaluate_isolated(
            """(sel) => {
              const el = document.querySelector(sel);
              if (el) {
                el.dispatchEvent(new Event('change', { bubbles: true }));
              }
            }""",
            selector,
        )

    def type(
        self, selector: str, text: str, *, clear: bool = False, timeout: float = 15.0
    ) -> None:
        self.click(selector, timeout=timeout)
        if clear:
            self._human.press_combo("Control+a", self._dispatch_key)
            self._human.press_combo("Backspace", self._dispatch_key)
        self._human.type_text(text, self._dispatch_key, self._insert_text)

    def press(
        self, selector: str, key: str, *, timeout: float = 15.0
    ) -> None:
        self.click(selector, timeout=timeout)
        self._human.press_combo(key, self._dispatch_key)

    def screenshot(
        self,
        *,
        path: str | Path | None = None,
        full_page: bool = False,
        clip: dict[str, float] | None = None,
        scale: str = "device",
        image_format: str = "png",
    ) -> bytes:
        if scale not in ("device", "css"):
            raise ValueError("scale must be 'device' or 'css'")
        dpr = float(self.evaluate_isolated("window.devicePixelRatio || 1"))
        params: dict[str, Any] = {"format": image_format}
        if full_page:
            params["captureBeyondViewport"] = True
            metrics = self._cdp.send("Page.getLayoutMetrics", session_id=self.session_id)
            cs = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            w = float(cs.get("width") or 1280)
            h = float(cs.get("height") or 720)
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": w,
                "height": h,
                "scale": (1.0 / dpr) if scale == "css" else 1.0,
            }
        elif clip is not None:
            params["clip"] = {
                "x": float(clip["x"]),
                "y": float(clip["y"]),
                "width": float(clip["width"]),
                "height": float(clip["height"]),
                "scale": (1.0 / dpr) if scale == "css" else 1.0,
            }
        elif scale == "css" and dpr != 1.0:
            vw, vh = self._viewport_size()
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": vw,
                "height": vh,
                "scale": 1.0 / dpr,
            }

        res = self._cdp.send(
            "Page.captureScreenshot", params, session_id=self.session_id
        )
        raw = base64.b64decode(res["data"])
        if path is not None:
            out_path = Path(path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(raw)
        return raw

    def _emit_mouse_move(self, x: float, y: float) -> None:
        self._dispatch_mouse(
            type="mouseMoved",
            x=float(x),
            y=float(y),
            button="none",
            buttons=0,
            modifiers=self._human.modifier_mask,
            timestamp=time.time(),
        )

    def _emit_wheel(
        self, x: float, y: float, delta_x: float, delta_y: float
    ) -> None:
        self._dispatch_mouse(
            type="mouseWheel",
            x=float(x),
            y=float(y),
            deltaX=float(delta_x),
            deltaY=float(delta_y),
            modifiers=self._human.modifier_mask,
            timestamp=time.time(),
        )

    def _dispatch_mouse(self, **kwargs: Any) -> None:
        payload = {k: v for k, v in kwargs.items() if v is not None}
        self._cdp.send(
            "Input.dispatchMouseEvent", payload, session_id=self.session_id
        )

    def _dispatch_key(self, **kwargs: Any) -> None:
        payload = {k: v for k, v in kwargs.items() if v is not None}
        self._cdp.send(
            "Input.dispatchKeyEvent", payload, session_id=self.session_id
        )

    def _insert_text(self, text: str) -> None:
        self._cdp.send(
            "Input.insertText", {"text": text}, session_id=self.session_id
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsub()
        try:
            self._cdp.send("Target.closeTarget", {"targetId": self.target_id}, timeout=5.0)
        except Exception:
            pass
