# -*- coding: utf-8 -*-
"""OSR_Smart_HUB pwr 驱动（6 路供电开关 + V/I 测量 + 过流检测；hidapi）。

- pwr 层首实现（tool-integration.md §5.1 P1）：崩板检测（电流掉底）与
  逐端口断电止损的现实来源；
- C1 骨架阶段用 MockSmartHub：负载模型可注入（假崩板电流掉底）、
  mark_trigger/valley 为 judge C3（电流谷值深度+时刻）提供数据；
- 真机层 TODO(P1)：from_serial() 延迟 import shub_ctrl（hidapi 同设备
  单进程独占——多 Agent 并发时的互斥约束）。
"""
from __future__ import annotations

import time
from typing import Any, Callable

LOAD_NORMAL = "normal"
LOAD_CRASH = "crash"          # 假崩板：电流掉底（<30% 基线 -> judge crash 信号链）
LOAD_OC = "oc"                # 过流（state=oc，互锁止损演练）


class MockSmartHub:
    """shub_ctrl 行为等价 Mock。

    设备侧等价：6 通道 set_power on/off；read_measurements 返回
    [{channel, state: on|off|oc, voltage_mv, current_ma}]。

    Mock 侧可注入：
    - set_load_mode("normal"|"crash"|"oc")：假负载（崩板电流掉底 / 过流）；
    - set_load_model(fn)：完全自定义 fn(channel, on)->(state, voltage_mv, current_ma)；
    - mark_trigger()/valley：触发时刻打点 + 读测量历史中的 (谷值电流, 相对触发
      偏移 ms)——judge C3 数据源（真机为 10-50ms 轮询曲线，Mock 为读点粒度）。

    与真机差异：无 hidapi 独占约束、无真实 V/I 采样；轮询粒度由调用方决定。
    """

    def __init__(
        self,
        *,
        channels: int = 6,
        serial: str = "MOCKSHUB0000",
        baseline_ma: float = 42.0,
        voltage_mv: float = 5000.0,
    ) -> None:
        self.n_channels = int(channels)
        self.serial = str(serial)
        self.baseline_ma = float(baseline_ma)
        self.voltage_mv = float(voltage_mv)
        self.power: dict[int, bool] = {ch: False for ch in range(1, self.n_channels + 1)}
        self.load_mode: str = LOAD_NORMAL
        self.load_model: Callable[[int, bool], tuple[str, float, float]] | None = None
        self.reads = 0
        self._history: list[tuple[float, int, str, float, float]] = []
        self._trigger_t: float | None = None
        self._valley: tuple[float | None, float | None] = (None, None)
        self.calls: list[tuple[str, tuple, dict]] = []

    # ---- 设备原语（与 shub_ctrl 同签名风格）----

    def set_power(self, channel: int, on: bool) -> None:
        ch = int(channel)
        if not (1 <= ch <= self.n_channels):
            raise ValueError(f"channel 超出 1-{self.n_channels}: {ch}")
        self.calls.append(("set_power", (ch, bool(on)), {}))
        self.power[ch] = bool(on)

    def read_measurements(self, channel: int = 0) -> list[dict[str, Any]]:
        """读端口测量；channel=0 表示全部通道。"""
        self.calls.append(("read_measurements", (int(channel),), {}))
        chs = range(1, self.n_channels + 1) if int(channel) == 0 else [int(channel)]
        out: list[dict[str, Any]] = []
        now = time.monotonic()
        for ch in chs:
            state, v_mv, i_ma = self._model(ch, self.power[ch])
            entry = {
                "channel": ch,
                "state": state,
                "voltage_mv": v_mv,
                "current_ma": i_ma,
            }
            out.append(entry)
            self._history.append((now, ch, state, v_mv, i_ma))
            # C3 谷值：自触发打点以来的电流最小值与其相对偏移
            if self._trigger_t is not None and self.power.get(ch):
                v_ma, _v_off = self._valley
                if v_ma is None or i_ma < v_ma:
                    self._valley = (i_ma, (now - self._trigger_t) * 1000.0)
        self.reads += 1
        return out

    # ---- Mock 侧 ----

    def set_load_mode(self, mode: str) -> None:
        """注入假负载模式：normal / crash（电流掉底）/ oc（过流）。"""
        if mode not in (LOAD_NORMAL, LOAD_CRASH, LOAD_OC):
            raise ValueError(f"未知负载模式: {mode!r}")
        self.load_mode = mode

    def set_load_model(self, fn: Callable[[int, bool], tuple[str, float, float]]) -> None:
        """注入完全自定义负载模型 fn(channel, on)->(state, voltage_mv, current_ma)。"""
        self.load_model = fn

    def mark_trigger(self) -> None:
        """触发时刻打点（C3 谷值偏移的 0 点；每射由 executor 调用）。"""
        self._trigger_t = time.monotonic()
        self._valley = (None, None)

    @property
    def valley(self) -> tuple[float | None, float | None]:
        """(valley_ma, valley_offset_ms)：自 mark_trigger 以来的电流谷值（C3）。"""
        return self._valley

    def discover_devices(self) -> list[dict[str, Any]]:
        """设备枚举（Mock 恒返回自身一条）。"""
        return [{"serial": self.serial, "channels": self.n_channels, "kind": "osr_smart_hub"}]

    def _model(self, ch: int, on: bool) -> tuple[str, float, float]:
        if self.load_model is not None:
            return self.load_model(ch, on)
        if not on:
            return ("off", 0.0, 0.0)
        if self.load_mode == LOAD_OC:
            return ("oc", self.voltage_mv, self.baseline_ma * 50.0)
        if self.load_mode == LOAD_CRASH:
            # 崩板：电流掉底（~1% 基线，<30% 阈值 -> judge crash 信号）
            return ("on", self.voltage_mv, max(0.2, self.baseline_ma * 0.01))
        # normal：确定性小抖动（reads 计数派生，测试可复现）
        wobble = (self.reads * 7) % 5 * 0.2
        return ("on", self.voltage_mv, self.baseline_ma + wobble)


class SmartHubPwr:
    """PwrDriver 实现（包装 Mock 或真机；供电开关为物理动作，必落审计）。"""

    def __init__(self, device: Any, *, audit: Any = None,
                 actor: str = "hwctl.pwr.shub") -> None:
        self.device = device
        self.audit = audit
        self.actor = actor

    def list_ports(self) -> list[dict[str, Any]]:
        return list(self.device.discover_devices())

    def set_power(self, channel: int, on: bool) -> None:
        """逐端口供电开关（物理动作：上电/断电都落审计——E4 审计重放要求）。"""
        self.device.set_power(int(channel), bool(on))
        self._audit("pwr.set_power", f"shub[ch={channel}]", {"on": bool(on)})

    def read_measurements(self, channel: int = 0) -> list[dict[str, Any]]:
        """[{channel, state, voltage_mv, current_ma}]（高频读，不落审计）。"""
        return list(self.device.read_measurements(int(channel)))

    # ---- C3 数据源（Mock 侧扩展，真机以轮询曲线等价实现 TODO(P1)）----

    def mark_trigger(self) -> None:
        marker = getattr(self.device, "mark_trigger", None)
        if marker is not None:
            marker()

    @property
    def valley(self) -> tuple[float | None, float | None]:
        getter = getattr(self.device, "valley", None)
        if getter is None:
            return (None, None)
        return getter if isinstance(getter, tuple) else getter()

    def _audit(self, action: str, device: str, detail: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.record(self.actor, action, device, detail)

    @classmethod
    def from_serial(cls, serial: str, **kwargs: Any) -> "SmartHubPwr":
        """真机接入 TODO(P1)：延迟 import shub_ctrl（hidapi 同设备单进程独占）。"""
        try:
            import shub_ctrl  # noqa: 延迟 import
        except ImportError as exc:
            raise RuntimeError(
                "shub_ctrl 包未安装——真机层 TODO(P1)。"
                "C1 骨架请用 device=MockSmartHub()。"
            ) from exc
        device = shub_ctrl.SmartHubController(serial)
        return cls(device, **kwargs)
