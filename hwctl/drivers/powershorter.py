# -*- coding: utf-8 -*-
"""PowerShorter rig 驱动（rig 三 mode 之 voltage 优先；fi-integration.md §2.7 裁决）。

- **RigDriver 为规范接口**；FI fi/hardware/powershorter.py 的 PowerShorterInjector
  是本模块 voltage 模式的一个被包装实现。继承其语义：
  build_pattern [(1,delay),(0,pulse),(1,1)]、DURATION_UNIT_NS=10（10ns 单位）、
  _check_safety 熔断（脉宽上限/次数上限/连续失败熔断/紧急停止）、
  InjectionReceipt 回执（pattern 原样）。
- hwctl 补上 FI 缺的：relay/gpio 复位编排（**复位分档**：常规射 RESET 脉冲、
  仅 crash 才 0V 断电）、审计、安全域接缝。
- C1 骨架阶段用 MockPowerShorterDevice（行为等价：<=8 段 / level 0-1 / dur>=0
  校验、arm/state 状态机、可注入 fail_times 假失败与假崩溃）。
- 真机层 TODO(P0)：from_port() 延迟 import power_shorter（COM 口），
  继电器极性/电气参数待人工校验（tool-integration.md §5.3）。
"""
from __future__ import annotations

import time
from typing import Any, Sequence

from hwctl.protocol import (
    DURATION_UNIT_NS,
    UNIT_SOURCE,
    InjectionReceipt,
    build_glitch_pattern,
)
from hwctl.safety import SafetyViolation

#: 安全缺省（继承 FI 量级）
DEFAULT_MAX_PULSE_RAW = 100          # 1 us
DEFAULT_MAX_INJECTIONS = 10_000
DEFAULT_MAX_CONSECUTIVE_FAILURES = 20


class MockPowerShorterDevice:
    """OSR power_shorter 包的行为等价 Mock（无串口；记录全部调用；可注入故障）。

    行为等价点（同 FI MockPowerShorter / power_shorter docstring 设备事实）：
    - engine_cfg：pattern <=8 段、level ∈ {0,1}、duration >= 0（否则 ValueError）；
      trigger_edges/pattern_repeat >= 1；
    - arm(engine, init_level) -> state 'armed'；soft_trigger -> state 'glitched'；
    - gpio/relay：value ∈ {0,1}，记录电平（复位/断电编排的观测点）。

    可注入故障：
    - fail_times=N：前 N 次软触发失败（验证连续失败熔断）；
    - crash_on=callable(delay_raw, pulse_raw)->bool：参数命中即标记"假崩溃"
      （last_crash 属性，供测试断言；真目标行为由 uart/shub mock 表达）。

    与真机的差异：无 10ns 真实时序与电气效应；时序由 rig 层 sleep 近似
    （测试可传 0 缩放）。
    """

    def __init__(
        self,
        *,
        fail_times: int = 0,
        crash_on: Any = None,
    ) -> None:
        self.fail_times = int(fail_times)
        self.crash_on = crash_on
        self.calls: list[tuple[str, tuple, dict]] = []
        self._armed: dict[int, bool] = {}
        self.relay_state: dict[int, int] = {}
        self.gpio_state: dict[int, int] = {}
        self.last_pattern: tuple[tuple[int, int], ...] | None = None
        self.last_shot: tuple[int, int] | None = None      # (delay_raw, pulse_raw)
        self.last_crash: bool = False

    # ---- 设备原语（与 power_shorter 包同签名）----

    def engine_cfg(self, engine: Any, pattern: Sequence[tuple[int, int]], trigger_mode: Any = None,
                   pattern_repeat: int = 1, trigger_edges: int = 1) -> None:
        self.calls.append(("engine_cfg", (engine, tuple(pattern)), {
            "trigger_mode": trigger_mode,
            "pattern_repeat": pattern_repeat,
            "trigger_edges": trigger_edges,
        }))
        if len(pattern) > 8:
            raise ValueError("at most 8 pattern can be set")
        for level, dur in pattern:
            if level not in (0, 1):
                raise ValueError(f"level 必须 0/1: {level}")
            if dur < 0:
                raise ValueError(f"duration 不得为负: {dur}")
        if trigger_edges < 1:
            raise ValueError("trigger_edges 必须 >= 1")
        if pattern_repeat < 1:
            raise ValueError("pattern_repeat 必须 >= 1")
        self.last_pattern = tuple(pattern)

    def arm(self, engine: Any, init_level: int = 0) -> None:
        self.calls.append(("arm", (engine,), {"init_level": init_level}))
        self._armed[engine] = True

    def soft_trigger(self, engine: Any) -> None:
        self.calls.append(("soft_trigger", (engine,), {}))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("mock: 软触发失败（模拟设备异常）")
        self._armed[engine] = False
        self.last_shot = self._decode_glitch_pattern(self.last_pattern)
        self.last_crash = bool(
            self.crash_on(*self.last_shot)
        ) if (self.crash_on is not None and self.last_shot is not None) else False

    def state(self, engine: Any) -> str:
        return "armed" if self._armed.get(engine) else "glitched"

    def gpio(self, idx: int, value: int) -> None:
        if value not in (0, 1):
            raise ValueError("gpio value must be 0 or 1")
        self.calls.append(("gpio", (idx, value), {}))
        self.gpio_state[int(idx)] = int(value)

    def relay(self, idx: int, value: int) -> None:
        if value not in (0, 1):
            raise ValueError("relay value must be 0 or 1")
        self.calls.append(("relay", (idx, value), {}))
        self.relay_state[int(idx)] = int(value)

    # ---- Mock 侧辅助 ----

    @staticmethod
    def _decode_glitch_pattern(pattern: tuple[tuple[int, int], ...] | None) -> tuple[int, int] | None:
        """[(1,d),(0,p),(1,1)] -> (d, p)；形状不符返回 None。"""
        if not pattern or len(pattern) != 3:
            return None
        if tuple(pattern[0])[0] != 1 or tuple(pattern[1])[0] != 0 or tuple(pattern[2]) != (1, 1):
            return None
        return (int(pattern[0][1]), int(pattern[1][1]))

    def crash_marker(self) -> bool:
        """最近一射是否命中假崩溃注入（测试断言用）。"""
        return self.last_crash


class PowerShorterRig:
    """RigDriver 规范实现（voltage mode 优先）。

    device 可为 MockPowerShorterDevice（C1 骨架 / CI）或真机薄封装（TODO P0）。
    安全熔断继承 FI _check_safety 语义：紧急停止 / 负值 / 脉宽超限 /
    次数超限 / 连续失败熔断——上限可收紧，不得无声放宽。
    """

    def __init__(
        self,
        device: Any,
        *,
        engine: int = 0,
        trigger_mode: Any = 0,
        init_level: int = 1,
        max_pulse_raw: int = DEFAULT_MAX_PULSE_RAW,
        max_injections: int = DEFAULT_MAX_INJECTIONS,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        reset_gpio: int = 0,
        power_relay: int = 0,
        reset_pulse_s: float = 0.02,
        power_cycle_hold_s: float = 0.3,
        v_target_mv: int = 3300,
        unit_scale_ns: int = DURATION_UNIT_NS,
        audit: Any = None,
        actor: str = "hwctl.rig.powershorter",
    ) -> None:
        self.device = device
        self.engine = engine
        self.trigger_mode = trigger_mode
        self.init_level = init_level
        self.max_pulse_raw = int(max_pulse_raw)
        self.max_injections = int(max_injections)
        self.max_consecutive_failures = int(max_consecutive_failures)
        self.reset_gpio = int(reset_gpio)
        self.power_relay = int(power_relay)
        self.reset_pulse_s = float(reset_pulse_s)
        self.power_cycle_hold_s = float(power_cycle_hold_s)
        self.v_target_mv = int(v_target_mv)
        self.unit_scale_ns = int(unit_scale_ns)
        self.audit = audit
        self.actor = actor
        self._stopped = False
        self._n_injections = 0
        self._consecutive_failures = 0
        self._pending_pulse_raw: int | None = None

    # ---------------- 安全熔断（FI _check_safety 语义继承）----------------

    def _check_safety(self, pulse_raw: int) -> None:
        if self._stopped:
            raise SafetyViolation("rig_emergency_stopped", "已紧急停止，拒绝继续注入（需显式 reset_safety()）")
        if pulse_raw < 0:
            raise SafetyViolation("rig_pulse_negative", f"pulse_raw 不得为负: {pulse_raw}")
        if pulse_raw > self.max_pulse_raw:
            raise SafetyViolation(
                "rig_pulse_exceeds_limit",
                f"脉宽超限: pulse_raw={pulse_raw} > 上限 {self.max_pulse_raw}"
                f"（= {self.max_pulse_raw * DURATION_UNIT_NS} ns）",
            )
        if self._n_injections >= self.max_injections:
            raise SafetyViolation(
                "rig_max_injections",
                f"注入次数超限: {self._n_injections} >= {self.max_injections}",
            )
        if self._consecutive_failures >= self.max_consecutive_failures:
            raise SafetyViolation(
                "rig_consecutive_failures",
                f"连续失败熔断: {self._consecutive_failures} 次连续失败",
            )

    def emergency_stop(self) -> None:
        """紧急停止：拒绝后续注入。"""
        self._stopped = True

    def reset_safety(self) -> None:
        """显式解除紧急停止（需人类确认后调用）。"""
        self._stopped = False
        self._consecutive_failures = 0

    # ---------------- 参数映射（FI 语义）----------------

    @staticmethod
    def build_pattern(delay_raw: int, pulse_raw: int) -> list[tuple[int, int]]:
        """FI build_pattern 语义：[(1,delay),(0,pulse),(1,1)]。"""
        return build_glitch_pattern(delay_raw, pulse_raw)

    @staticmethod
    def to_ns(value_raw: int) -> int:
        return int(value_raw) * DURATION_UNIT_NS

    # ---------------- RigDriver 六件套 ----------------

    def configure(
        self,
        mode: str = "voltage",
        pattern: Sequence[tuple[int, int]] | None = None,
        *,
        trigger_mode: Any = None,
        trigger_edges: int = 1,
        pattern_repeat: int = 1,
        gpio_preset: dict[int, int] | None = None,
        relay_preset: dict[int, int] | None = None,
    ) -> None:
        """配置注入模式与 pattern（六件套 1）。emfi/clock 真机映射 TODO(P0)。"""
        if mode != "voltage":
            # TODO(P0): EMPulser/CycleWarper 模式映射（同包三 mode，签名同构）
            raise NotImplementedError(f"mode={mode!r} 真机映射 TODO(P0)（C1 骨架只接 voltage）")
        if pattern is None:
            raise ValueError("pattern 必填")
        # 从 glitch pattern 反解 pulse_raw 做熔断检查（形状不符则跳过 rig 级检查）
        decoded = MockPowerShorterDevice._decode_glitch_pattern(tuple(pattern))
        if decoded is not None:
            self._check_safety(decoded[1])
            self._pending_pulse_raw = decoded[1]
        self.device.engine_cfg(
            self.engine, pattern,
            trigger_mode=self.trigger_mode if trigger_mode is None else trigger_mode,
            pattern_repeat=pattern_repeat,
            trigger_edges=trigger_edges,
        )
        if gpio_preset:
            for idx, value in gpio_preset.items():
                self.device.gpio(int(idx), int(value))
        if relay_preset:
            for idx, value in relay_preset.items():
                self.device.relay(int(idx), int(value))
        self._audit("rig.configure", self._device_name(), {
            "mode": mode, "pattern": [list(seg) for seg in pattern],
            "trigger_edges": trigger_edges, "pattern_repeat": pattern_repeat,
        })

    def arm(self) -> None:
        """布防（六件套 2）。"""
        self.device.arm(self.engine, init_level=self.init_level)
        self._audit("rig.arm", self._device_name(), {"engine": int(self.engine)})

    def state(self) -> str:
        """设备状态（六件套 3）："armed" | "glitched"。"""
        return str(self.device.state(self.engine))

    def reset_target(self, *, mode: str = "reset_pulse", hold_s: float | None = None) -> None:
        """目标复位（六件套 4）。复位分档（scan-acceleration.md §3.2）：

        - "reset_pulse"：GPIO RESET 脉冲（常规射；10-50ms 量级）；
        - "power_cycle"：机械继电器 0V 断电保持（仅 crash 后；~300ms，OSR-2560 手册法）。
        """
        if mode == "reset_pulse":
            hold = self.reset_pulse_s if hold_s is None else float(hold_s)
            self.device.gpio(self.reset_gpio, 0)
            time.sleep(hold)
            self.device.gpio(self.reset_gpio, 1)
        elif mode == "power_cycle":
            hold = self.power_cycle_hold_s if hold_s is None else float(hold_s)
            self.device.relay(self.power_relay, 0)
            time.sleep(hold)
            self.device.relay(self.power_relay, 1)
        else:
            raise ValueError(f"未知复位档: {mode!r}（reset_pulse | power_cycle）")
        self._audit("rig.reset_target", self._device_name(), {"mode": mode, "hold_s": hold})

    def soft_trigger(self) -> InjectionReceipt:
        """软触发一次注入并返回回执（六件套 5 的 MVP 路径）。

        硬件触发链（UartGuarder TRI -> trigger_edges 边沿触发）接入后，
        射击改由硬件边沿起爆，本方法仅用于自检（TODO P0）。
        """
        pulse_raw = self._pending_pulse_raw
        delay_raw = 0
        if self.device.last_pattern:
            decoded = MockPowerShorterDevice._decode_glitch_pattern(self.device.last_pattern)
            if decoded is not None:
                delay_raw, pulse_raw = decoded
        receipt = InjectionReceipt(
            delay_raw=int(delay_raw),
            pulse_raw=int(pulse_raw or 0),
            delay_ns=self.to_ns(delay_raw),
            pulse_ns=self.to_ns(pulse_raw or 0),
            pattern=[tuple(seg) for seg in (self.device.last_pattern or ())],
            engine=int(self.engine) if isinstance(self.engine, int) else repr(self.engine),
            trigger_mode=str(self.trigger_mode),
            ok=False,
            unit_scale_ns=self.unit_scale_ns,
            unit_source=UNIT_SOURCE,
        )
        try:
            self.device.soft_trigger(self.engine)
            receipt.ok = True
            self._consecutive_failures = 0
        except Exception as exc:  # noqa: BLE001 —— 设备异常要记录而非吞掉（FI 语义）
            receipt.error = f"{type(exc).__name__}: {exc}"
            self._consecutive_failures += 1
        finally:
            self._n_injections += 1
            self._audit("rig.inject", self._device_name(), {
                "delay_raw": receipt.delay_raw,
                "pulse_raw": receipt.pulse_raw,
                "ok": receipt.ok,
                "error": receipt.error,
            })
        return receipt

    def inject(self, delay_raw: int, pulse_raw: int) -> InjectionReceipt:
        """FI 语义的便捷单射：build_pattern -> configure -> arm -> soft_trigger。"""
        self._check_safety(int(pulse_raw))
        pattern = self.build_pattern(delay_raw, pulse_raw)
        self.configure("voltage", pattern)
        self.arm()
        return self.soft_trigger()

    # ---------------- 内部 ----------------

    def _device_name(self) -> str:
        return f"powershorter[engine={self.engine}]"

    def _audit(self, action: str, device: str, detail: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.record(self.actor, action, device, detail)

    @classmethod
    def from_port(cls, port: str, **kwargs: Any) -> "PowerShorterRig":
        """真机接入 TODO(P0)：延迟 import power_shorter（无设备环境不触发）。

        接线/继电器极性待人工校验（tool-integration.md §5.3 Top1）。
        """
        try:
            import power_shorter as ps  # noqa: 延迟 import，保证无设备环境可测试
        except ImportError as exc:
            raise RuntimeError(
                "power_shorter 包未安装——真机层 TODO(P0)。"
                "C1 骨架请用 device=MockPowerShorterDevice()。"
            ) from exc
        device = ps.PowerShorter(port)  # 真机参数（timeout 等）待人工校验
        return cls(device, **kwargs)
