# -*- coding: utf-8 -*-
"""判定器：C1/C2/C3（+C5）通道采集 + outcome + heat_score（scan-acceleration.md §4）。

outcome ∈ success | crash | no_effect | other（tool-integration.md §3.2 判定闭环）：
- crash：ping 无应答 +（boot 重试 > 0 或 电流掉底 < 30% 基线）；
- success：目标存活且应答形态异常（C2 diff != exact——最便宜的"部分成功"探测器）；
- no_effect：目标存活且金标应答完全一致；
- other：无法归入以上（如 ping 超时但电流正常——超时自适应 v1 修正对象）。

通道（§4.1，全部判定器自动采集，不依赖人眼）：
- C1 resp_latency_ms：注入后 UART ping 往返时间；
- C2 resp_diff_form：金标应答 diff：exact/subset/extra_tokens/wrong_chars/silent（0-4）；
- C3 valley_ma / valley_offset_ms：电流谷值深度与相对触发时刻；
- C4（轨塌陷，scope 抽样射）缺省关闭，v1 接波形特征提取；
- C5 boot_retry_count：判存活所需重试次数。

热区分（§4.3）：S = 100*success + Σ w_i*z_i（clip 0-8，MAD 归一）；
crash 过渡带（15）/平台（3）为**批级**项，v1 随热图批次合成落地（heat_score 列
先落每射部分）。基线（50 空射 median/MAD，§4.2）接口留好：无基线时 z=0。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from hwctl.protocol import InjectionReceipt, PingResult
from hwctl.scan.params import BASE_CHANNEL_WEIGHTS, ParamPoint

#: outcome 枚举（tool-integration.md §3.2 / domain-map）
OUTCOMES: tuple[str, ...] = ("success", "crash", "no_effect", "other")

#: C2 应答 diff 形态（§4.1 有序离散，编码 0-4）
RESP_DIFF_FORMS: tuple[str, ...] = ("exact", "subset", "extra_tokens", "wrong_chars", "silent")


def _is_subsequence(small: bytes, big: bytes) -> bool:
    it = iter(big)
    return all(c in it for c in small)


def classify_diff(expected: bytes | None, actual: bytes | None) -> str:
    """金标应答 diff 形态分类（C2；简化规则，真机 token 级 diff v1 增强）。

    - actual 为空/None -> silent（无应答）；
    - actual == expected -> exact；
    - actual 是 expected 的子序列（丢了 token）-> subset；
    - expected 是 actual 的子序列（多了 token）-> extra_tokens；
    - 金标缺失 -> wrong_chars（无法确认一致，按异常处理）；
    - 其余 -> wrong_chars。
    """
    if actual is None or len(actual) == 0:
        return "silent"
    if expected is None:
        return "wrong_chars"
    if actual == expected:
        return "exact"
    if _is_subsequence(actual, expected):
        return "subset"
    if _is_subsequence(expected, actual):
        return "extra_tokens"
    return "wrong_chars"


def diff_code(form: str) -> int:
    """C2 形态 -> 有序编码 0-4（落台账 resp_diff_code 列）。"""
    return RESP_DIFF_FORMS.index(form)


@dataclass
class Verdict:
    """判定器 -> 执行器/台账的通道向量（§6.2 边界：只回通道向量）。"""

    outcome: str
    channels: dict[str, dict[str, Any]]
    state_label: str | None = None
    """FI campaign-record 对齐：数据源原始状态标签字面量（语义 unconfirmed-v0.1；
    Mock 无数据源标签 -> None，不臆造）。"""
    responded: bool | None = None
    heat_score: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class BaselineStats:
    """空射基线（扫描前 50 空射 median/MAD，§4.2）。"""

    median: dict[str, float] = field(default_factory=dict)
    mad: dict[str, float] = field(default_factory=dict)

    def z(self, key: str, value: float | None) -> float:
        """z = clip((x - median)/MAD, 0, 8)；缺基线或 MAD=0 -> 0.0。"""
        if value is None:
            return 0.0
        med = self.median.get(key)
        mad = self.mad.get(key)
        if med is None or not mad:
            return 0.0
        z = (value - med) / mad
        return max(0.0, min(8.0, z))


class ShotJudge:
    """判定器接口（before_shot 钩子 + judge 通道向量）。"""

    def before_shot(self, point: ParamPoint) -> None:
        """每射前的场景/标定钩子（可把假目标行为写进 mock 设备）。"""

    def judge(
        self,
        point: ParamPoint,
        receipt: InjectionReceipt,
        *,
        reset_mode: str | None = None,
    ) -> Verdict:
        raise NotImplementedError


class MockShotJudge(ShotJudge):
    """Mock 判定器：优先 script；否则走 Mock 信号链（uart ping + shub 电流）推导。

    - script: (point, receipt) -> Verdict（完全接管，测试用）；
    - prepare: (point) -> None，在 before_shot 调用（场景把假崩溃/假应答写进
      uart/shub mock——判定链路真实走过信号，而非直接回字符串）；
    - 信号推导规则见模块 docstring。

    与真机判定器的差异：真机 ping 走 UartGuarder 反向 UART（真实 RTT）、
    C3 走 shub 10-50ms 轮询曲线、超时自适应（p99x1.5 逐批修正）——Mock 全部
    以固定值/单点读近似，判定**规则**一致。
    """

    def __init__(
        self,
        *,
        uart: Any = None,
        pwr: Any = None,
        baseline: BaselineStats | None = None,
        channel_weights: Mapping[str, float] | None = None,
        script: Callable[[ParamPoint, InjectionReceipt], Verdict] | None = None,
        prepare: Callable[[ParamPoint], None] | None = None,
        ping_timeout_ms: int = 200,
        crash_current_ratio: float = 0.3,
    ) -> None:
        self.uart = uart
        self.pwr = pwr
        self.baseline = baseline
        self.weights = dict(BASE_CHANNEL_WEIGHTS if channel_weights is None else channel_weights)
        self.script = script
        self.prepare = prepare
        self.ping_timeout_ms = int(ping_timeout_ms)
        self.crash_current_ratio = float(crash_current_ratio)

    def before_shot(self, point: ParamPoint) -> None:
        if self.prepare is not None:
            self.prepare(point)

    def judge(
        self,
        point: ParamPoint,
        receipt: InjectionReceipt,
        *,
        reset_mode: str | None = None,
    ) -> Verdict:
        if self.script is not None:
            return self.script(point, receipt)
        return self._judge_from_signals(point, receipt, reset_mode=reset_mode)

    # ---------------- 信号链推导 ----------------

    def _judge_from_signals(
        self,
        point: ParamPoint,
        receipt: InjectionReceipt,
        *,
        reset_mode: str | None,
    ) -> Verdict:
        ping = PingResult(responded=False, error="uart-missing")
        golden: bytes | None = None
        if self.uart is not None:
            ping = self.uart.ping(b"PING", timeout_ms=self.ping_timeout_ms)
            golden = getattr(self.uart, "golden_response", None)
        form = classify_diff(golden, ping.response_bytes)

        current_ma: float | None = None
        valley_ma: float | None = None
        valley_off_ms: float | None = None
        if self.pwr is not None:
            ms = self.pwr.read_measurements()
            on_currents = [
                m.get("current_ma") for m in ms
                if m.get("state") == "on" and isinstance(m.get("current_ma"), (int, float))
            ]
            if on_currents:
                current_ma = float(min(on_currents))
            v_ma, v_off = getattr(self.pwr, "valley", (None, None))
            valley_ma = float(v_ma) if v_ma is not None else None
            valley_off_ms = float(v_off) if v_off is not None else None

        pwr_dev = getattr(self.pwr, "device", self.pwr)  # 穿过 PwrDriver 包装拿 Mock 原件
        baseline_current = getattr(pwr_dev, "baseline_ma", None) if self.pwr else None
        current_dropped = (
            current_ma is not None
            and baseline_current
            and current_ma < self.crash_current_ratio * float(baseline_current)
        )
        crashed = (not ping.responded) and (
            ping.boot_retry_count > 0 or current_dropped
        )
        if crashed:
            outcome = "crash"
        elif ping.responded:
            outcome = "no_effect" if form == "exact" else "success"
        else:
            outcome = "other"

        channels: dict[str, dict[str, Any]] = {
            "C1": {"resp_latency_ms": ping.latency_ms},
            "C2": {"resp_diff_form": form, "resp_diff_code": diff_code(form)},
            "C3": {"valley_ma": valley_ma, "valley_offset_ms": valley_off_ms},
            "C4": {},  # 缺省关闭（抽样射才开，v1 接 scope 特征提取）
            "C5": {"boot_retry_count": ping.boot_retry_count},
        }
        heat = self._heat_score(outcome, channels)
        return Verdict(
            outcome=outcome,
            channels=channels,
            state_label=None,   # Mock 无数据源标签，不臆造（FI 纪律）
            responded=ping.responded,
            heat_score=round(heat, 3),
            detail={"resp_diff_form": form, "reset_mode": reset_mode},
        )

    def _heat_score(self, outcome: str, channels: Mapping[str, Mapping[str, Any]]) -> float:
        """每射热分（§4.3 每射部分）：100*success + Σ w_i*z_i。

        无基线 -> z 全 0（heat = 100*success）；crash 过渡带/平台为批级项，
        v1 随热图批次合成（TODO 注记，§4.3）。
        """
        score = 100.0 if outcome == "success" else 0.0
        b = self.baseline
        z1 = b.z("resp_latency_ms", channels["C1"].get("resp_latency_ms")) if b else 0.0
        z2 = b.z("resp_diff_code", channels["C2"].get("resp_diff_code")) if b else 0.0
        z3 = b.z("valley_ma", channels["C3"].get("valley_ma")) if b else 0.0
        z5 = b.z("boot_retry_count", channels["C5"].get("boot_retry_count")) if b else 0.0
        score += self.weights.get("C1", 0.0) * z1
        score += self.weights.get("C2", 0.0) * z2
        score += self.weights.get("C3", 0.0) * z3
        score += self.weights.get("C5", 0.0) * z5
        # TODO(v1): + 15*crash_transition(格邻域) + 3*crash_plateau(格)（批级合成）
        return score


class ScriptedScenario:
    """确定性场景（dry-run / 测试用）：按 (delay, pulse) 相对位置决定假目标行为。

    把假崩溃/假应答写进 uart/shub mock（prepare 钩子），判定链路走完整信号：
    - crash 平台：pulse 相对位置 >= crash_pulse_ratio -> uart silent_crash
      + shub 电流掉底（假崩溃）；
    - success 带：delay/pulse 落在窗口中央带（默认 delay ∈ [0.35,0.55] x span、
      pulse ∈ [0.15,0.45] x span）-> uart mutated（错字符应答 -> C2 -> success）；
    - 其余 no_effect：uart normal（金标应答一致）。

    纯确定性（无随机），同一 grid 重放结果一致——dry-run 可作为回归基线。
    """

    def __init__(
        self,
        window: ParamWindow,
        *,
        uart: Any = None,
        pwr: Any = None,
        success_band: tuple[tuple[float, float], tuple[float, float]] = ((0.35, 0.55), (0.15, 0.45)),
        crash_pulse_ratio: float = 0.8,
    ) -> None:
        self.window = window
        self.uart = uart
        self.pwr = pwr
        self.success_band = success_band
        self.crash_pulse_ratio = float(crash_pulse_ratio)

    def _rel(self, axis: str, point: ParamPoint) -> float | None:
        try:
            a = self.window.axis(axis)
        except KeyError:
            return None
        for unit in ("raw", "ns"):
            v = point.try_get(axis, unit)
            if v is not None:
                span = a.max_value - a.min_value
                return (float(v) - a.min_value) / span if span > 0 else 0.0
        return None

    def prepare(self, point: ParamPoint) -> None:
        """before_shot 钩子：把本射的假目标行为写进 mock 设备。"""
        d_rel = self._rel("delay", point)
        p_rel = self._rel("pulse", point)
        p_ok = p_rel is not None
        d_ok = d_rel is not None
        in_success = (
            d_ok and p_ok
            and self.success_band[0][0] <= d_rel <= self.success_band[0][1]
            and self.success_band[1][0] <= p_rel <= self.success_band[1][1]
        )
        in_crash = p_ok and p_rel >= self.crash_pulse_ratio
        pwr_dev = getattr(self.pwr, "device", self.pwr)  # 穿过 PwrDriver 包装拿 Mock 原件
        set_mode = getattr(pwr_dev, "set_load_mode", None)
        if in_crash:
            if self.uart is not None:
                self.uart.set_target("silent_crash")   # 假崩溃：无应答 + boot 重试
            if set_mode is not None:
                set_mode("crash")                       # 假崩板：电流掉底
        elif in_success:
            if self.uart is not None:
                self.uart.set_target("mutated")         # 假部分成功：错字符应答
            if set_mode is not None:
                set_mode("normal")
        else:
            if self.uart is not None:
                self.uart.set_target("normal")
            if set_mode is not None:
                set_mode("normal")
