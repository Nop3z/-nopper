# -*- coding: utf-8 -*-
"""hwctl 驱动协议层（tool-integration.md §2.1/§2.2 + scan 六件套）。

四类 Protocol：
- RigDriver     注入（PowerShorter 三 mode：voltage/emfi/clock）
- PwrDriver     供电与电流监测（shub_ctrl 优先，ucctl power 次之）
- ScopeDriver   采集（pico3000 首实现，五驱动统一抽象）
- TriggerDriver 协议触发（UartGuarder 字节串触发为主链触发源）

scan 六件套（executor 驱动设备的统一生命周期序）：
    configure -> arm -> state -> reset_target -> capture -> download
各驱动实现其中适用子集（映射见各类 docstring）。

InjectionReceipt 继承 FI fi/hardware/powershorter.py 的审计回执语义
（pattern 原样 / unit_scale / confidence），加时间戳字段。
fi-integration.md §2.7 裁决：RigDriver 为规范接口，
FI PowerShorterInjector 是其 power_shorter 模式的一个被包装实现。

单位纪律（泛化自 fi/loop/types.py）：参数键名显式携带单位 {axis}_{unit}；
raw->ns 换算率必须显式给出（unit_scale）并落台账（unit_scale/unit_source 列），
不做任何隐式换算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence, runtime_checkable

#: PowerShorter 设备 duration 单位（ns/单位）。
#: 设备事实，来自 power_shorter.engine_cfg docstring：duration is in unit of 10ns。
DURATION_UNIT_NS = 10

#: 单位出处（落台账 unit_source 列，避免隐式假设——FI 教训）。
UNIT_SOURCE = "power_shorter.engine_cfg docstring: duration is in unit of 10ns"


def utc_now_iso() -> str:
    """UTC ISO-8601 时间戳（毫秒精度，带时区）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class InjectionReceipt:
    """一次注入的完整回执（审计与复现用）。

    继承 FI InjectionReceipt 全部字段（pattern 原样 / unit_scale / confidence），
    增加 timestamp 字段。pattern **原样**落台账是审计铁律。
    """

    delay_raw: int
    pulse_raw: int
    delay_ns: int
    pulse_ns: int
    pattern: list[tuple[int, int]]
    engine: int
    trigger_mode: str
    ok: bool
    error: str | None = None
    unit_scale_ns: int = DURATION_UNIT_NS
    unit_source: str = UNIT_SOURCE
    confidence: str = "observation"  # 参数为实测下发的，属观测（FI 语义）
    timestamp: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PingResult:
    """反向 UART ping 的一次观测（judge C1/C2/C5 通道的数据源）。

    tool-integration.md §3：射击后目标存活检查 = UartGuarder 反向 UART ping。
    """

    responded: bool
    latency_ms: float | None = None       # C1：ping 往返时延
    response_bytes: bytes | None = None   # C2：金标应答 diff 的原料
    boot_retry_count: int = 0             # C5：判存活所需的重试次数
    error: str | None = None


@dataclass(frozen=True)
class ShotRequest:
    """一次射击的设备级参数（safety 域检查的输入；request_from_point 构造）。"""

    delay_raw: int
    pulse_raw: int
    delay_ns: int
    pulse_ns: int
    pattern: tuple[tuple[int, int], ...]
    v_target_mv: int = 3300
    unit_scale_ns: int = DURATION_UNIT_NS


def build_glitch_pattern(delay_raw: int, pulse_raw: int) -> list[tuple[int, int]]:
    """把 (delay, pulse) 映射为 PowerShorter 设备 pattern（继承 FI build_pattern 语义）。

    [(1, delay_raw), (0, pulse_raw), (1, 1)]：
    保持导通 delay 个 10ns 单位 -> 断开 pulse 个单位（毛刺）-> 恢复导通。
    level=1 = MOSFET 导通（正常供电）；level=0 = MOSFET 断开（掉电 = 毛刺）；
    最后一段电平保持到下次 arm（设备事实）。
    """
    return [(1, int(delay_raw)), (0, int(pulse_raw)), (1, 1)]


def request_from_point(
    point: Any,
    *,
    unit_scale_ns: int = DURATION_UNIT_NS,
    v_target_mv: int = 3300,
) -> ShotRequest:
    """从 ParamPoint（{axis}_{unit} 泛化键）构造 ShotRequest。

    优先取 delay_raw / pulse_raw（设备原刻度）；只有 ns 时按 unit_scale
    显式换算（换算率来自调用方，不隐式假设）。
    """
    d_raw = point.try_get("delay", "raw")
    p_raw = point.try_get("pulse", "raw")
    d_ns = point.try_get("delay", "ns")
    p_ns = point.try_get("pulse", "ns")
    if d_raw is None and d_ns is None:
        raise ValueError("参数点缺少 delay 轴（delay_raw 或 delay_ns）")
    if p_raw is None and p_ns is None:
        raise ValueError("参数点缺少 pulse 轴（pulse_raw 或 pulse_ns）")
    delay_raw = int(d_raw) if d_raw is not None else int(round(float(d_ns) / unit_scale_ns))
    pulse_raw = int(p_raw) if p_raw is not None else int(round(float(p_ns) / unit_scale_ns))
    delay_ns = int(d_ns) if d_ns is not None else delay_raw * unit_scale_ns
    pulse_ns = int(p_ns) if p_ns is not None else pulse_raw * unit_scale_ns
    return ShotRequest(
        delay_raw=delay_raw,
        pulse_raw=pulse_raw,
        delay_ns=delay_ns,
        pulse_ns=pulse_ns,
        pattern=tuple(build_glitch_pattern(delay_raw, pulse_raw)),
        v_target_mv=v_target_mv,
        unit_scale_ns=unit_scale_ns,
    )


#: scan 六件套（executor 统一驱动序；各驱动实现适用子集）。
SHOT_LIFECYCLE = ("configure", "arm", "state", "reset_target", "capture", "download")


@runtime_checkable
class RigDriver(Protocol):
    """注入驱动（PowerShorter）。

    六件套映射：configure / arm / state / reset_target 全实现；
    capture/download 不适用（波形归 ScopeDriver）。

    触发两路（tool-integration.md §3）：
    - 硬件触发（主链）：UartGuarder TRI -> 本设备 trigger_edges 边沿触发，同源闭环；
    - 软触发：soft_trigger()（MVP / Mock 路径）。
    """

    def configure(
        self,
        mode: str,
        pattern: Sequence[tuple[int, int]],
        *,
        trigger_mode: Any = None,
        trigger_edges: int = 1,
        pattern_repeat: int = 1,
        gpio_preset: dict[int, int] | None = None,
        relay_preset: dict[int, int] | None = None,
    ) -> Any:
        """配置注入模式与 pattern。mode ∈ voltage|emfi|clock。"""
        ...

    def arm(self) -> None:
        """布防（等待触发）。"""
        ...

    def state(self) -> str:
        """设备状态："armed" | "glitched"。"""
        ...

    def reset_target(self, *, mode: str = "reset_pulse", hold_s: float | None = None) -> None:
        """目标复位。复位分档（scan-acceleration.md §3.2）：

        - "reset_pulse"：RESET 线脉冲（常规射，10-50ms 量级）；
        - "power_cycle"：继电器 0V 断电保持 ~300ms（**仅 crash 后走此档**）。
        """
        ...

    def soft_trigger(self) -> InjectionReceipt:
        """软触发一次注入并返回回执（硬件触发链 TRI 接入前的 MVP 路径）。"""
        ...

    def inject(self, delay_raw: int, pulse_raw: int) -> InjectionReceipt:
        """FI 语义的便捷单射：build_pattern -> configure -> arm -> soft_trigger。"""
        ...


@runtime_checkable
class PwrDriver(Protocol):
    """供电/监测驱动（shub_ctrl 优先；tool-integration.md §2.2 签名）。

    六件套映射：set_power 承担 reset_target 的 0V 断电档（crash 止损阶梯）。
    """

    def list_ports(self) -> list[dict[str, Any]]:
        """枚举端口（对应 shub_ctrl.discover_devices）。"""
        ...

    def set_power(self, channel: int, on: bool) -> None:
        """逐端口供电开关；互锁止损的物理抓手（物理动作，必落审计）。"""
        ...

    def read_measurements(self, channel: int = 0) -> list[dict[str, Any]]:
        """[{channel, state: on|off|oc, voltage_mv, current_ma}]；0 = 全部通道。"""
        ...


@runtime_checkable
class ScopeDriver(Protocol):
    """采集驱动（pico3000 首实现；五驱动统一抽象，tool-integration.md §2.1）。

    六件套映射：configure / arm / state / capture / download 全实现；
    reset_target 不适用（目标复位归 RigDriver/PwrDriver）。
    """

    def configure(
        self,
        channels: Sequence[str],
        *,
        sample_rate_hz: float,
        duration_s: float | None = None,
        samples: int | None = None,
        pre_trigger_ratio: float = 0.25,
        trigger_source: str = "EXT",
        trigger_threshold_mv: int = 0,
        trigger_direction: str = "rise",
        vrange_mv: float = 2000.0,
    ) -> None:
        """通道/触发/采集三合一配置（EXT = 外部触发，统一各家枚举差异）。"""
        ...

    def arm(self) -> None:
        """布防（预触发窗口须覆盖注入延时全程——同源触发三原则）。"""
        ...

    def state(self) -> str:
        """设备状态："idle" | "armed" | "captured"。"""
        ...

    def capture(self, channels: Sequence[str] | None = None) -> dict[str, Any]:
        """拉波形。返回 {"samples": {ch: [float]}, "sample_rate_hz", "n_samples", ...}。"""
        ...

    def download(self, path: str, fmt: str = "csv") -> str:
        """产物落盘（evidence 用），返回实际写出路径。"""
        ...

    def close(self) -> None:
        """释放设备。"""
        ...


@runtime_checkable
class TriggerDriver(Protocol):
    """协议触发驱动（UartGuarder 字节串触发为主链触发源）。

    六件套映射：configure / arm / state 实现；
    反向 UART ping（ping）是 judge 目标存活检查的数据源。
    """

    def configure(
        self,
        *,
        match_bytes: bytes,
        baud: int = 115200,
        engine: int = 1,
        data_bits: int = 8,
        parity: str = "none",
        stop_bits: int = 1,
    ) -> None:
        """字节串匹配配置（1-8B；波特率 2400-115200）。"""
        ...

    def arm(self) -> None:
        """布防（TRI 输出使能）。"""
        ...

    def state(self) -> str:
        """设备状态："idle" | "armed" | "triggered"。"""
        ...

    def ping(self, payload: bytes, timeout_ms: int = 200) -> PingResult:
        """反向 UART ping（tool-integration.md §3 目标存活检查）。"""
        ...
