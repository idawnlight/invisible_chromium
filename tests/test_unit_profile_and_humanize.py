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
    assert seal["version"] == "157.0.8103.50"
    asset = seal["assets"]["linux-x86_64"]
    assert len(asset["sha256"]) == 64
    assert asset["size_bytes"] > 100_000_000
    assert asset["entry_rel"] == "chrome"


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
