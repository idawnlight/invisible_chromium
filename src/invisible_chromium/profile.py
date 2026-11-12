"""Linux Desktop hardware profile configuration for patched Chromium."""
from __future__ import annotations

from dataclasses import dataclass
import json
import random
from typing import Any

from invisible_core.seedmix import sub_seed

VALID_DEVICE_MEMORY_GIB = (2, 4, 8, 16, 32)

# Authentic Linux Desktop Intel Mesa (`iris`) OpenGL 4.6 GPU strings.
# Every entry in this pool shares the exact Mesa `iris` OpenGL 4.6 capability
# envelope enforced by 0002-webgl-desktop-normalization.patch:
# MAX_TEXTURE_SIZE=16384, MAX_3D_TEXTURE_SIZE=2048, ALIASED_LINE_WIDTH_RANGE=[1, 7.375],
# ALIASED_POINT_SIZE_RANGE=[1, 255], MAX_SAMPLES=8, MAX_ELEMENTS_INDICES=1048576,
# MIN/MAX_PROGRAM_TEXEL_OFFSET=-8/7, UNIFORM_BUFFER_OFFSET_ALIGNMENT=64,
# LOW_INT/MEDIUM_INT=[31, 30, 0], and `#version 450` translated GLSL shaders.
LINUX_INTEL_MESA_RENDERERS: tuple[tuple[str, str], ...] = (
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) Xe Graphics (TGL GT2), OpenGL 4.6)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) Graphics (ADL GT2), OpenGL 4.6)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) Graphics (RPL-S), OpenGL 4.6)",
    ),
)

# Realistic (cores, ram_gb) combinations weighted toward mainstream Linux desktops/laptops.
LINUX_DESKTOP_SPECS: tuple[tuple[int, int, int], ...] = (
    # (hardware_concurrency, device_memory_gb, weight)
    (8, 8, 30),
    (8, 16, 35),
    (12, 16, 15),
    (16, 16, 8),
    (16, 32, 7),
    (4, 8, 5),
)


def derive_cpu_performance(cores: int) -> int:
    if cores <= 2:
        return 1
    if cores <= 4:
        return 2
    if cores <= 12:
        return 3
    return 4


@dataclass(frozen=True)
class DesktopHardwareProfile:
    """Immutable hardware profile serialized into INVISIBLE_CHROMIUM_PROFILE."""

    hardware_concurrency: int = 8
    device_memory_gb: int = 8
    cpu_performance: int | None = None
    normalize_webgl: bool = True
    webgl_vendor: str = "Google Inc. (Intel)"
    webgl_renderer: str = (
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)"
    )

    def __post_init__(self) -> None:
        if not isinstance(self.hardware_concurrency, int) or isinstance(
            self.hardware_concurrency, bool
        ) or self.hardware_concurrency < 1:
            raise ValueError("hardware_concurrency must be a positive integer")
        if self.device_memory_gb not in VALID_DEVICE_MEMORY_GIB:
            raise ValueError(
                f"device_memory_gb must be one of {VALID_DEVICE_MEMORY_GIB}"
            )
        if self.cpu_performance is not None:
            if (
                not isinstance(self.cpu_performance, int)
                or isinstance(self.cpu_performance, bool)
                or not (0 <= self.cpu_performance <= 4)
            ):
                raise ValueError("cpu_performance must be in [0, 4]")
        if not isinstance(self.normalize_webgl, bool):
            raise ValueError("normalize_webgl must be a bool")
        if not self.webgl_vendor or not self.webgl_renderer:
            raise ValueError("webgl_vendor and webgl_renderer must be non-empty")

    @property
    def effective_cpu_performance(self) -> int:
        if self.cpu_performance is not None:
            return self.cpu_performance
        return derive_cpu_performance(self.hardware_concurrency)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hardwareConcurrency": self.hardware_concurrency,
            "deviceMemory": self.device_memory_gb,
            "cpuPerformance": self.effective_cpu_performance,
            "normalizeWebgl": self.normalize_webgl,
            "webglVendor": self.webgl_vendor,
            "webglRenderer": self.webgl_renderer,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_seed(cls, seed: int) -> "DesktopHardwareProfile":
        rng = random.Random(sub_seed(seed, "chromium:desktop-hardware"))
        total_weight = sum(w for _, _, w in LINUX_DESKTOP_SPECS)
        pick = rng.uniform(0, total_weight)
        acc = 0.0
        cores, ram_gb = 8, 8
        for c, r, w in LINUX_DESKTOP_SPECS:
            acc += w
            if pick <= acc:
                cores, ram_gb = c, r
                break
        vendor, renderer = rng.choice(LINUX_INTEL_MESA_RENDERERS)
        return cls(
            hardware_concurrency=cores,
            device_memory_gb=ram_gb,
            cpu_performance=derive_cpu_performance(cores),
            normalize_webgl=True,
            webgl_vendor=vendor,
            webgl_renderer=renderer,
        )
