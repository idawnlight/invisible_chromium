"""Seed-deterministic human motion, clicking, scrolling, and typing over CDP."""
from __future__ import annotations

from collections.abc import Callable
import random
import time
from typing import Any

from invisible_core.juggler import (
    CursorMotion,
    Ev,
    PageActs,
    PointerPersona,
    Step,
    clamp_to_viewport,
    fit_timeline,
    initial_pointer,
    keylayout,
    landing_point,
    plan_approach,
    plan_idle,
    plan_scroll,
    steps_from_waypoints,
    tail_within,
)
from invisible_core.juggler._behaviour import (
    TypingPersona,
    plan_click,
    plan_press_settle,
    plan_shift,
    plan_typing,
)
from invisible_core.juggler._pacing import drive as drive_timeline
from invisible_core.seedmix import sub_seed

# Chromium CDP modifier bitmask (differs from Juggler: Meta=4, Shift=8 in CDP).
CDP_MODIFIERS = {
    "Alt": 1,
    "Control": 2,
    "Meta": 4,
    "Shift": 8,
}

CDP_BUTTON_MASK = {
    "none": 0,
    "left": 1,
    "right": 2,
    "middle": 4,
}

KEY_ALIASES = {
    "ShiftLeft": ("Shift",),
    "ControlLeft": ("Control", "Ctrl"),
    "AltLeft": ("Alt",),
    "MetaLeft": ("Meta", "Command", "Super"),
    "Enter": ("\n", "\r"),
}


def _build_key_table() -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for code, entry in keylayout.LAYOUT.items():
        key = entry.get("key") or ""
        vk = entry.get("keyCodeWithoutLocation") or entry.get("keyCode") or 0
        base_descr = {
            "code": code,
            "key": key,
            "windowsVirtualKeyCode": vk,
            "location": entry.get("location") or 0,
            "text": key if len(key) == 1 else (entry.get("text") or ""),
            "requires_shift": False,
        }
        shifted_descr = None
        if entry.get("shiftKey"):
            s_key = entry["shiftKey"]
            s_vk = entry.get("shiftKeyCode") or vk
            shifted_descr = {
                "code": code,
                "key": s_key,
                "windowsVirtualKeyCode": s_vk,
                "location": entry.get("location") or 0,
                "text": s_key if len(s_key) == 1 else "",
                "requires_shift": True,
            }
        table[code] = base_descr
        for alias in KEY_ALIASES.get(code, ()):
            table[alias] = base_descr
        if not entry.get("location"):
            if key and key not in table:
                table[key] = base_descr
            if shifted_descr and shifted_descr["key"] not in table:
                table[shifted_descr["key"]] = shifted_descr
    # Ensure Enter carries \r text on keyDown so forms submit and textareas newline.
    if "Enter" in table:
        enter = dict(table["Enter"], text="\r")
        table["Enter"] = enter
        table["\n"] = enter
        table["\r"] = enter
    return table


KEY_TABLE = _build_key_table()


def steps_to_events(
    steps: list[Step],
    viewport: tuple[float, float],
    *,
    clamp_endpoints: bool = False,
) -> list[Ev]:
    evs: list[Ev] = []
    t_acc = 0.0
    vw, vh = viewport
    for idx, step in enumerate(steps):
        t_acc += max(float(step.delay_ms), 0.0)
        is_last = idx == len(steps) - 1
        x, y = float(step.x), float(step.y)
        if not (is_last and not clamp_endpoints):
            x, y = clamp_to_viewport(x, y, vw, vh)
        evs.append(
            Ev(
                t_ms=t_acc,
                x=x,
                y=y,
                kind=step.kind,
                dx=float(getattr(step, "dx", 0.0) or 0.0),
                dy=float(getattr(step, "dy", 0.0) or 0.0),
            )
        )
    return evs


class HumanController:
    """Per-page human motion and typing controller backed by invisible_core."""

    def __init__(
        self,
        *,
        seed: int,
        page_number: int = 1,
        enabled: bool = True,
        max_motion_seconds: float = 1.2,
    ) -> None:
        self.seed = int(seed)
        self.enabled = bool(enabled)
        self.max_motion_seconds = float(max_motion_seconds)
        self.acts = PageActs(page_number)
        self.pointer_persona = PointerPersona.from_seed(self.seed)
        self.typing_persona = TypingPersona.from_seed(self.seed)
        self.motion = CursorMotion(sub_seed(self.seed, f"chromium:cursor:{page_number}"))
        self._pos: tuple[float, float] | None = None
        self._last_action_mono: float | None = None
        self._active_modifiers: set[str] = set()

    @property
    def modifier_mask(self) -> int:
        mask = 0
        for mod in self._active_modifiers:
            mask |= CDP_MODIFIERS.get(mod, 0)
        return mask

    def ensure_position(self, viewport: tuple[float, float]) -> tuple[float, float]:
        if self._pos is None:
            self._pos = initial_pointer(
                sub_seed(self.seed, f"chromium:init:{self.acts.page}"), viewport
            )
        return self._pos

    def _render_stroke(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        kind: str,
        lead_ms: float = 0.0,
        bounds: tuple[float, float] | None = None,
        target_w: float | None = None,
    ) -> list[Step]:
        waypoints = self.motion.path(
            start[0], start[1], end[0], end[1], target_w=target_w
        )
        return steps_from_waypoints(
            waypoints, kind=kind, lead_ms=lead_ms, bounds=bounds
        )

    def move_to_point(
        self,
        x: float,
        y: float,
        viewport: tuple[float, float],
        emit_move: Callable[[float, float], None],
    ) -> tuple[float, float]:
        target = (float(x), float(y))
        origin = self.ensure_position(viewport)
        if not self.enabled or self.max_motion_seconds <= 0:
            emit_move(target[0], target[1])
            self._pos = target
            self._last_action_mono = time.monotonic()
            return target

        steps = self._render_stroke(origin, target, kind="move", bounds=viewport)
        evs = fit_timeline(steps_to_events(steps, viewport), self.max_motion_seconds)
        if evs:
            drive_timeline(evs, emit_move, origin=origin)
        else:
            emit_move(target[0], target[1])
        self._pos = target
        self._last_action_mono = time.monotonic()
        return target

    def approach_box(
        self,
        box: tuple[float, float, float, float],
        viewport: tuple[float, float],
        emit_move: Callable[[float, float], None],
    ) -> tuple[float, float]:
        origin = self.ensure_position(viewport)
        nonce = self.acts.next("approach")
        rng = random.Random(sub_seed(self.seed, f"chromium:landing:{nonce}"))
        target_pt = landing_point(box, rng, spread=0.14, keep=0.26)

        if not self.enabled or self.max_motion_seconds <= 0:
            emit_move(target_pt[0], target_pt[1])
            self._pos = target_pt
            self._last_action_mono = time.monotonic()
            return target_pt

        combined_steps: list[Step] = []
        now_mono = time.monotonic()
        if self._last_action_mono is not None:
            gap_ms = min((now_mono - self._last_action_mono) * 1000.0, 45_000.0)
            if gap_ms >= 400.0:
                idle_steps = plan_idle(
                    self.seed,
                    self.pointer_persona,
                    origin,
                    viewport,
                    duration_ms=gap_ms,
                    nonce=nonce,
                    render=self._render_stroke,
                )
                idle_tail = tail_within(
                    idle_steps, self.max_motion_seconds * 1000.0 * 0.30
                )
                if idle_tail:
                    combined_steps.extend(idle_tail)
                    origin = (idle_tail[-1].x, idle_tail[-1].y)

        approach_steps = plan_approach(
            self.seed,
            self.pointer_persona,
            origin,
            box,
            viewport,
            nonce=nonce,
            landing=target_pt,
            render=self._render_stroke,
        )
        combined_steps.extend(approach_steps)
        evs = fit_timeline(
            steps_to_events(combined_steps, viewport), self.max_motion_seconds
        )
        if evs:
            drive_timeline(evs, emit_move, origin=self._pos)
        else:
            emit_move(target_pt[0], target_pt[1])

        self._pos = target_pt
        self._last_action_mono = time.monotonic()
        return target_pt

    def perform_click(
        self,
        x: float,
        y: float,
        *,
        button: str = "left",
        click_count: int = 1,
        dispatch_mouse: Callable[..., None],
    ) -> None:
        nonce = self.acts.next("click")
        btn_mask = CDP_BUTTON_MASK.get(button, 1)
        if self.enabled:
            settle_ms = plan_press_settle(self.pointer_persona, nonce=nonce)
            time.sleep(min(settle_ms, 180.0) / 1000.0)
            presses = plan_click(self.pointer_persona, clicks=click_count, nonce=nonce)
        else:
            presses = [(12.0, 20.0 if i < click_count - 1 else 0.0) for i in range(click_count)]

        for idx, (dwell_ms, gap_ms) in enumerate(presses, start=1):
            dispatch_mouse(
                type="mousePressed",
                x=x,
                y=y,
                button=button,
                buttons=btn_mask,
                clickCount=idx,
                modifiers=self.modifier_mask,
                timestamp=time.time(),
            )
            if dwell_ms > 0:
                time.sleep(min(dwell_ms, 250.0) / 1000.0)
            dispatch_mouse(
                type="mouseReleased",
                x=x,
                y=y,
                button=button,
                buttons=0,
                clickCount=idx,
                modifiers=self.modifier_mask,
                timestamp=time.time(),
            )
            if gap_ms > 0:
                time.sleep(min(gap_ms, 300.0) / 1000.0)

        self._pos = (float(x), float(y))
        self._last_action_mono = time.monotonic()

    def scroll_wheel(
        self,
        delta_x: float,
        delta_y: float,
        viewport: tuple[float, float],
        emit_move: Callable[[float, float], None],
        emit_wheel: Callable[[float, float, float, float], None],
    ) -> None:
        origin = self.ensure_position(viewport)
        if not self.enabled or abs(delta_y) < 1.0 or abs(delta_x) > 0.0:
            emit_wheel(origin[0], origin[1], delta_x, delta_y)
            self._last_action_mono = time.monotonic()
            return

        nonce = self.acts.next("scroll")
        sign = 1.0 if delta_y >= 0 else -1.0
        ticks = max(1, min(12, round(abs(delta_y) / 100.0)))
        tick_dy = (abs(delta_y) / ticks) * sign
        steps = plan_scroll(
            self.seed,
            self.pointer_persona,
            origin,
            viewport,
            ticks=ticks,
            tick_dy=tick_dy,
            nonce=nonce,
            render=self._render_stroke,
        )
        evs = fit_timeline(
            steps_to_events(steps, viewport, clamp_endpoints=True),
            max(self.max_motion_seconds, 1.5),
        )
        cur_pos = [origin[0], origin[1]]

        def _on_move(mx: float, my: float) -> None:
            cur_pos[0], cur_pos[1] = mx, my
            emit_move(mx, my)

        def _on_wheel(wx: float, wy: float) -> None:
            emit_wheel(cur_pos[0], cur_pos[1], wx, wy)

        drive_timeline(evs, _on_move, emit_wheel=_on_wheel, origin=origin)
        self._pos = (cur_pos[0], cur_pos[1])
        self._last_action_mono = time.monotonic()

    def type_text(
        self,
        text: str,
        dispatch_key: Callable[..., None],
        insert_text_fn: Callable[[str], None],
    ) -> None:
        if not text:
            return
        nonce = self.acts.next("typing")
        timings = (
            plan_typing(text, self.typing_persona, nonce=nonce)
            if self.enabled
            else [(10.0, 5.0) for _ in text]
        )

        # Count contiguous Shift runs for realistic shift lead/lag timing.
        shift_runs = 0
        prev_shift = False
        for ch in text:
            info = KEY_TABLE.get(ch)
            needs_shift = bool(info and info["requires_shift"])
            if needs_shift and not prev_shift:
                shift_runs += 1
            prev_shift = needs_shift
        shift_timings = (
            plan_shift(self.typing_persona, shift_runs, nonce=nonce)
            if (self.enabled and shift_runs > 0)
            else [(8.0, 8.0) for _ in range(shift_runs)]
        )
        shift_idx = 0
        shift_held = False

        for idx, ch in enumerate(text):
            dwell_ms, gap_ms = timings[idx]
            info = KEY_TABLE.get(ch)
            if info is None:
                if shift_held:
                    self._release_modifier("Shift", dispatch_key)
                    shift_held = False
                insert_text_fn(ch)
                if gap_ms > 0 and self.enabled:
                    time.sleep(min(gap_ms, 250.0) / 1000.0)
                continue

            needs_shift = bool(info["requires_shift"])
            if needs_shift and not shift_held:
                lead_ms, _ = shift_timings[min(shift_idx, len(shift_timings) - 1)]
                self._press_modifier("Shift", dispatch_key)
                shift_held = True
                if lead_ms > 0 and self.enabled:
                    time.sleep(min(lead_ms, 120.0) / 1000.0)
            elif not needs_shift and shift_held:
                _, lag_ms = shift_timings[min(shift_idx, len(shift_timings) - 1)]
                shift_idx += 1
                if lag_ms > 0 and self.enabled:
                    time.sleep(min(lag_ms, 120.0) / 1000.0)
                self._release_modifier("Shift", dispatch_key)
                shift_held = False

            char_text = info["text"]
            dispatch_key(
                type="keyDown" if char_text else "rawKeyDown",
                key=info["key"],
                code=info["code"],
                windowsVirtualKeyCode=info["windowsVirtualKeyCode"],
                nativeVirtualKeyCode=info["windowsVirtualKeyCode"],
                location=info["location"],
                modifiers=self.modifier_mask,
                text=char_text or None,
                unmodifiedText=(char_text.lower() if char_text else None),
                timestamp=time.time(),
            )
            if dwell_ms > 0 and self.enabled:
                time.sleep(min(dwell_ms, 200.0) / 1000.0)
            dispatch_key(
                type="keyUp",
                key=info["key"],
                code=info["code"],
                windowsVirtualKeyCode=info["windowsVirtualKeyCode"],
                nativeVirtualKeyCode=info["windowsVirtualKeyCode"],
                location=info["location"],
                modifiers=self.modifier_mask,
                timestamp=time.time(),
            )

            next_needs_shift = (
                idx + 1 < len(text)
                and bool((KEY_TABLE.get(text[idx + 1]) or {}).get("requires_shift"))
            )
            if shift_held and not next_needs_shift:
                _, lag_ms = shift_timings[min(shift_idx, len(shift_timings) - 1)]
                shift_idx += 1
                if lag_ms > 0 and self.enabled:
                    time.sleep(min(lag_ms, 120.0) / 1000.0)
                self._release_modifier("Shift", dispatch_key)
                shift_held = False

            if gap_ms > 0 and self.enabled:
                time.sleep(min(gap_ms, 300.0) / 1000.0)

        self._last_action_mono = time.monotonic()

    def press_combo(
        self, combo: str, dispatch_key: Callable[..., None]
    ) -> None:
        parts = [p for p in combo.split("+") if p] if "+" in combo and combo != "+" else [combo]
        mods: list[str] = []
        main_key = parts[-1]
        for mod_part in parts[:-1]:
            canonical = {
                "Ctrl": "Control",
                "Control": "Control",
                "Shift": "Shift",
                "Alt": "Alt",
                "Meta": "Meta",
                "Command": "Meta",
            }.get(mod_part, mod_part)
            mods.append(canonical)
            self._press_modifier(canonical, dispatch_key)

        try:
            info = KEY_TABLE.get(main_key)
            if info is None:
                raise ValueError(f"Unknown key name: {main_key!r}")
            has_ctrl_alt_meta = any(m in ("Control", "Alt", "Meta") for m in mods)
            char_text = "" if has_ctrl_alt_meta else info["text"]
            dispatch_key(
                type="keyDown" if char_text else "rawKeyDown",
                key=info["key"],
                code=info["code"],
                windowsVirtualKeyCode=info["windowsVirtualKeyCode"],
                nativeVirtualKeyCode=info["windowsVirtualKeyCode"],
                location=info["location"],
                modifiers=self.modifier_mask,
                text=char_text or None,
                unmodifiedText=info["text"] or None,
                timestamp=time.time(),
            )
            if self.enabled:
                time.sleep(0.025)
            dispatch_key(
                type="keyUp",
                key=info["key"],
                code=info["code"],
                windowsVirtualKeyCode=info["windowsVirtualKeyCode"],
                nativeVirtualKeyCode=info["windowsVirtualKeyCode"],
                location=info["location"],
                modifiers=self.modifier_mask,
                timestamp=time.time(),
            )
        finally:
            for mod in reversed(mods):
                self._release_modifier(mod, dispatch_key)
        self._last_action_mono = time.monotonic()

    def _press_modifier(self, mod: str, dispatch_key: Callable[..., None]) -> None:
        info = KEY_TABLE[mod]
        self._active_modifiers.add(mod)
        dispatch_key(
            type="rawKeyDown",
            key=info["key"],
            code=info["code"],
            windowsVirtualKeyCode=info["windowsVirtualKeyCode"],
            nativeVirtualKeyCode=info["windowsVirtualKeyCode"],
            location=info["location"],
            modifiers=self.modifier_mask,
            timestamp=time.time(),
        )

    def _release_modifier(self, mod: str, dispatch_key: Callable[..., None]) -> None:
        info = KEY_TABLE[mod]
        self._active_modifiers.discard(mod)
        dispatch_key(
            type="keyUp",
            key=info["key"],
            code=info["code"],
            windowsVirtualKeyCode=info["windowsVirtualKeyCode"],
            nativeVirtualKeyCode=info["windowsVirtualKeyCode"],
            location=info["location"],
            modifiers=self.modifier_mask,
            timestamp=time.time(),
        )
