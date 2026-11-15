# -*- coding: utf-8 -*-
"""pico3000 scope 驱动（六件套：configure / arm / state / capture / download + close）。

五驱动统一抽象（tool-integration.md §2.1）首实现：pico3000（PicoSDK ps3000a，
OSR-303/OSR-2560 官方推荐采集脚本，MVP 默认 scope）。

- C1 骨架阶段用 MockPico3000Scope：配置校验 + 确定性合成波形（无 PicoSDK 依赖）；
- C4（轨塌陷特征提取）v1 接入；MVP 抽样射只 download 存证（evidence），
  sag_depth_mv / sag_duration_us / rail_recovery_ms 列留空；
- 真机层 TODO(P0)：from_serial() 延迟 import pico3000。
"""
from __future__ import annotations

import csv
import math
import os
from typing import Any, Sequence

WAVEFORM_N_SAMPLES = 256


class MockPico3000Scope:
    """pico3000 行为等价 Mock（无 PicoSDK）。

    行为等价点：configure 校验（采样率 > 0、通道已知、触发源枚举）；
    idle -> arm -> 'armed' -> capture -> 'captured'；capture 返回确定性合成波形
    （正弦 + 可注入的触发点塌陷 notch，模拟轨塌陷 C4 场景）。
    与真机差异：无真实 ADC/USB 回读；波形为合成数据，只供链路与落盘测试。
    """

    def __init__(
        self,
        *,
        channels: tuple[str, ...] = ("A", "B", "C", "D"),
        sample_rate_hz: float = 125e6,
        simulate_sag: bool = False,
    ) -> None:
        self.known_channels = tuple(channels)
        self.default_sample_rate_hz = float(sample_rate_hz)
        self.simulate_sag = bool(simulate_sag)
        self.capture_count = 0
        self._state = "idle"
        self._cfg: dict[str, Any] = {}
        self._last_capture: dict[str, Any] | None = None
        self.calls: list[tuple[str, tuple, dict]] = []

    def configure(self, channels: Sequence[str], *, sample_rate_hz: float | None = None,
                  duration_s: float | None = None, samples: int | None = None,
                  pre_trigger_ratio: float = 0.25, trigger_source: str = "EXT",
                  trigger_threshold_mv: int = 0, trigger_direction: str = "rise",
                  vrange_mv: float = 2000.0) -> None:
        self.calls.append(("configure", (tuple(channels),), {"sample_rate_hz": sample_rate_hz}))
        chs = tuple(channels)
        if not chs:
            raise ValueError("channels 不能为空")
        for ch in chs:
            if ch not in self.known_channels:
                raise ValueError(f"未知通道: {ch}（known={self.known_channels}）")
        sr = float(self.default_sample_rate_hz if sample_rate_hz is None else sample_rate_hz)
        if sr <= 0:
            raise ValueError(f"sample_rate_hz 必须 > 0: {sr}")
        if trigger_source not in ("EXT",) + self.known_channels:
            raise ValueError(f"触发源非法: {trigger_source}")
        if trigger_direction not in ("rise", "fall"):
            raise ValueError(f"触发方向非法: {trigger_direction}")
        self._cfg = {
            "channels": chs,
            "sample_rate_hz": sr,
            "samples": int(samples) if samples is not None else WAVEFORM_N_SAMPLES,
            "pre_trigger_ratio": float(pre_trigger_ratio),
            "trigger_source": trigger_source,
            "trigger_threshold_mv": int(trigger_threshold_mv),
            "trigger_direction": trigger_direction,
            "vrange_mv": float(vrange_mv),
        }
        self._state = "idle"

    def arm(self) -> None:
        self.calls.append(("arm", (), {}))
        if not self._cfg:
            raise RuntimeError("scope 未 configure，不能 arm")
        self._state = "armed"

    def state(self) -> str:
        return self._state

    def capture(self, channels: Sequence[str] | None = None) -> dict[str, Any]:
        """拉波形：确定性合成（正弦 + 可选触发点塌陷 notch）。"""
        self.calls.append(("capture", (), {}))
        if self._state != "armed":
            raise RuntimeError("scope 未 arm，不能 capture（六件套顺序约束）")
        cfg = self._cfg
        chs = tuple(channels) if channels is not None else cfg["channels"]
        n = int(cfg["samples"])
        samples_out: dict[str, list[float]] = {}
        for ch in chs:
            samples_out[ch] = self._synth_waveform(n)
        self.capture_count += 1
        self._state = "captured"
        result = {
            "samples": samples_out,
            "sample_rate_hz": cfg["sample_rate_hz"],
            "n_samples": n,
            "pre_trigger_samples": int(n * cfg["pre_trigger_ratio"]),
            "trigger_source": cfg["trigger_source"],
            "meta": {"mock": True, "capture_index": self.capture_count},
        }
        self._last_capture = result
        return result

    def download(self, path: str, fmt: str = "csv") -> str:
        """最近一次 capture 落盘（evidence 存证）。"""
        self.calls.append(("download", (path, fmt), {}))
        if self._last_capture is None:
            raise RuntimeError("无已 capture 的波形可下载")
        data = self._last_capture
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            chs = list(data["samples"].keys())
            w.writerow(["time_s"] + chs)
            n = data["n_samples"]
            sr = data["sample_rate_hz"]
            for i in range(n):
                w.writerow([i / sr] + [data["samples"][ch][i] for ch in chs])
        return path

    def close(self) -> None:
        self._state = "idle"

    def _synth_waveform(self, n: int) -> list[float]:
        vals: list[float] = []
        notch_center = n // 2
        for i in range(n):
            v = 1.0 + 0.05 * math.sin(2.0 * math.pi * i / 32.0)
            if self.simulate_sag and abs(i - notch_center) <= 4:
                v = 0.2  # 合成轨塌陷（C4 场景；特征提取 v1 TODO）
            vals.append(round(v, 6))
        return vals


class Pico3000Scope:
    """ScopeDriver 实现（包装 Mock 或真机；审计采样动作）。"""

    def __init__(self, device: Any, *, audit: Any = None,
                 actor: str = "hwctl.scope.pico3000") -> None:
        self.device = device
        self.audit = audit
        self.actor = actor

    def configure(self, channels: Sequence[str], **kwargs: Any) -> None:
        self.device.configure(channels, **kwargs)
        self._audit("scope.configure", "pico3000", {
            "channels": list(channels),
            "sample_rate_hz": kwargs.get("sample_rate_hz"),
            "trigger_source": kwargs.get("trigger_source", "EXT"),
        })

    def arm(self) -> None:
        self.device.arm()
        self._audit("scope.arm", "pico3000", {})

    def state(self) -> str:
        return str(self.device.state())

    def capture(self, channels: Sequence[str] | None = None) -> dict[str, Any]:
        data = self.device.capture(channels)
        self._audit("scope.capture", "pico3000", {
            "n_samples": data.get("n_samples"),
            "channels": list(data.get("samples", {}).keys()),
        })
        return data

    def download(self, path: str, fmt: str = "csv") -> str:
        out = self.device.download(path, fmt)
        self._audit("scope.download", "pico3000", {"path": out, "fmt": fmt})
        return out

    def close(self) -> None:
        self.device.close()

    def _audit(self, action: str, device: str, detail: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.record(self.actor, action, device, detail)

    @classmethod
    def from_serial(cls, serial: str | None = None, **kwargs: Any) -> "Pico3000Scope":
        """真机接入 TODO(P0)：延迟 import pico3000（无设备环境不触发）。"""
        try:
            import pico3000 as p3k  # noqa: 延迟 import
        except ImportError as exc:
            raise RuntimeError(
                "pico3000 包未安装——真机层 TODO(P0)。"
                "C1 骨架请用 device=MockPico3000Scope()。"
            ) from exc
        device = p3k.Pico3000A(serial=serial) if serial else p3k.Pico3000A()
        return cls(device, **kwargs)
