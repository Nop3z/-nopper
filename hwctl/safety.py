# -*- coding: utf-8 -*-
"""安全域互锁（fork-plan §8：安全域互锁 + CSV 台账 + 审计日志都在 hwctl 层）。

条令约定：
- 安全域配置来自 JSON 文件（默认 ./safety-domain.json），**Agent 不可写**——
  本模块只提供只读加载，不存在任何写入/保存接口；
- 越界即拒绝并落审计（谁在何时对什么设备做了什么）；
- **XOR 掩码不变量进安全域**（fi-integration.md §2.7；FI 教训）：
  bit_mask_on_reg_value 必须恰好等于 source XOR target，
  多写/少写一位都说明声明与意图不一致，机器拒绝。

缺省档（随包 safety-domain.default.json）是保守起点；现场按台架人工定值，
逐工具白名单（v_target 上限、pattern 最长段/总能量、每轨道最大连续 crash 数）
见 tool-integration.md §2.2 尾注。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from hwctl.protocol import DURATION_UNIT_NS, ShotRequest

SCHEMA = "hwctl.safety-domain-v0"
DEFAULT_CONFIG_FILENAME = "safety-domain.json"
#: 随包只读缺省档（现场文件不存在时的保守回退；来源落审计）。
DEFAULT_CONFIG_RESOURCE = os.path.join(os.path.dirname(__file__), "safety-domain.default.json")

#: 与 FI fi/hardware/powershorter.py 对齐的安全缺省（继承其护栏量级）。
FI_DEFAULT_MAX_PULSE_RAW = 100              # 1 us
FI_DEFAULT_MAX_INJECTIONS = 10_000
FI_DEFAULT_MAX_CONSECUTIVE_FAILURES = 20


class SafetyViolation(RuntimeError):
    """超出安全边界（越界即拒绝）。携带机器可读 code 与违例明细。"""

    def __init__(
        self,
        code: str,
        message: str,
        violations: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.violations = violations or [{"code": code, "message": message}]


@dataclass(frozen=True)
class XorInvariant:
    """XOR 掩码不变量（进安全域；FI 教训）。

    语义：把寄存器值从 source 打到 target 时，声明的位掩码
    bit_mask_on_reg_value 必须恰好等于 source XOR target。
    """

    name: str
    source: int
    target: int
    bit_mask_on_reg_value: int

    def mismatch(self) -> str | None:
        """不一致时返回人读描述；一致返回 None。"""
        expected = self.source ^ self.target
        if self.bit_mask_on_reg_value != expected:
            return (
                f"xor 掩码不一致 [{self.name}]: bit_mask_on_reg_value="
                f"{self.bit_mask_on_reg_value} != source^target={expected} "
                f"(source={self.source}, target={self.target})"
            )
        return None


def validate_xor_mask(
    source: int,
    target: int,
    mask: int,
    *,
    name: str = "unnamed",
    audit: Any = None,
) -> None:
    """XOR 掩码校验：mask 必须 == source ^ target，否则拒绝并落审计。"""
    inv = XorInvariant(
        name=name, source=int(source), target=int(target), bit_mask_on_reg_value=int(mask)
    )
    mismatch = inv.mismatch()
    if mismatch is not None:
        if audit is not None:
            audit.record(
                "hwctl.safety",
                "safety.xor_mask_reject",
                name,
                {
                    "source": inv.source,
                    "target": inv.target,
                    "bit_mask_on_reg_value": inv.bit_mask_on_reg_value,
                    "expected": inv.source ^ inv.target,
                },
            )
        raise SafetyViolation("xor_mask_mismatch", mismatch)


#: 配置必填键（缺一即拒绝加载——机器拒绝式，对齐 FI schemas 纪律）。
REQUIRED_KEYS: tuple[str, ...] = (
    "v_target_max_mv",
    "pattern_max_segments",
    "pattern_max_total_ns",
    "max_pulse_raw",
    "sense_ohms",
    "max_energy_uj",
    "max_consecutive_crash",
    "shot_budget",
)


@dataclass
class SafetyDomain:
    """安全域配置 + 运行时互锁状态（射击预算 / 连发 crash / 人工确认接缝）。

    运行时状态字段（shots_used / consecutive_crash / tripped_reason）不进配置文件。
    """

    # ---- 配置（JSON 只读加载）----
    v_target_max_mv: int = 3300
    pattern_max_segments: int = 8              # 设备硬限：<=8 段 pattern
    pattern_max_total_ns: int = 2_000_000      # pattern 总时长上限
    max_pulse_raw: int = FI_DEFAULT_MAX_PULSE_RAW
    sense_ohms: float = 50.0                   # 能量估算的 sense 电阻（声明假设）
    max_energy_uj: float = 500.0               # 单射能量上限（估算口径）
    max_consecutive_crash: int = FI_DEFAULT_MAX_CONSECUTIVE_FAILURES
    shot_budget: int = FI_DEFAULT_MAX_INJECTIONS
    unit_scale_ns: int = DURATION_UNIT_NS
    xor_invariants: tuple[XorInvariant, ...] = ()
    source_path: str | None = None

    # ---- 运行时互锁状态 ----
    shots_used: int = 0
    consecutive_crash: int = 0
    tripped_reason: str | None = None

    # ---------------- 只读加载 ----------------

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        source_path: str | None = None,
        audit: Any = None,
    ) -> "SafetyDomain":
        missing = [k for k in REQUIRED_KEYS if k not in data]
        if missing:
            raise SafetyViolation(
                "config_missing_keys", f"安全域配置缺必填键: {missing}"
            )
        if data.get("schema", SCHEMA) != SCHEMA:
            raise SafetyViolation(
                "config_schema_mismatch",
                f"schema 不符: {data.get('schema')!r} != {SCHEMA!r}",
            )
        invariants: list[XorInvariant] = []
        for raw in data.get("xor_invariants") or []:
            try:
                inv = XorInvariant(
                    name=str(raw.get("name", "unnamed")),
                    source=int(raw["source"]),
                    target=int(raw["target"]),
                    bit_mask_on_reg_value=int(raw["bit_mask_on_reg_value"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SafetyViolation(
                    "config_invalid_xor_invariant",
                    f"xor_invariants 条目形状非法: {raw!r}（{exc}）",
                ) from exc
            mismatch = inv.mismatch()
            if mismatch is not None:
                if audit is not None:
                    audit.record(
                        "hwctl.safety",
                        "safety.config_reject",
                        source_path or "<dict>",
                        {"reason": mismatch},
                    )
                raise SafetyViolation("xor_mask_mismatch", mismatch)
            invariants.append(inv)
        domain = cls(
            v_target_max_mv=int(data["v_target_max_mv"]),
            pattern_max_segments=int(data["pattern_max_segments"]),
            pattern_max_total_ns=int(data["pattern_max_total_ns"]),
            max_pulse_raw=int(data["max_pulse_raw"]),
            sense_ohms=float(data["sense_ohms"]),
            max_energy_uj=float(data["max_energy_uj"]),
            max_consecutive_crash=int(data["max_consecutive_crash"]),
            shot_budget=int(data["shot_budget"]),
            unit_scale_ns=int(data.get("unit_scale_ns", DURATION_UNIT_NS)),
            xor_invariants=tuple(invariants),
            source_path=source_path,
        )
        if audit is not None:
            audit.record(
                "hwctl.safety",
                "safety.config_loaded",
                source_path or "<dict>",
                {
                    "schema": SCHEMA,
                    "v_target_max_mv": domain.v_target_max_mv,
                    "shot_budget": domain.shot_budget,
                    "xor_invariants": len(invariants),
                },
            )
        return domain

    @classmethod
    def from_json_file(cls, path: str, *, audit: Any = None) -> "SafetyDomain":
        """只读加载安全域配置（Agent 不可写条令：本模块无任何写回接口）。"""
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_dict(data, source_path=path, audit=audit)

    @classmethod
    def load_default(cls, path: str | None = None, *, audit: Any = None) -> "SafetyDomain":
        """加载现场安全域（默认 ./safety-domain.json）；不存在则回退随包只读缺省档。

        回退事实落审计（来源可追溯）。
        """
        p = path or DEFAULT_CONFIG_FILENAME
        if os.path.exists(p):
            return cls.from_json_file(p, audit=audit)
        if audit is not None:
            audit.record(
                "hwctl.safety",
                "safety.config_fallback_default",
                p,
                {
                    "fallback": DEFAULT_CONFIG_RESOURCE,
                    "reason": "现场安全域文件不存在，使用随包只读缺省档（保守起点）",
                },
            )
        return cls.from_json_file(DEFAULT_CONFIG_RESOURCE, audit=audit)

    # ---------------- 检查 ----------------

    def estimate_energy_uj(self, request: ShotRequest) -> float:
        """单射能量估算（声明假设，非测量值）：level==0 段 ≈ 目标经 sense 电阻放电。

        E = V^2/R * t（对 pattern 逐段求和）。量级估算用于互锁阈值判断；
        实测能量标定属 v1 scope 通道（C4）工作。
        """
        if self.sense_ohms <= 0:
            return 0.0
        v = request.v_target_mv / 1000.0
        seconds = (
            sum(d for lvl, d in request.pattern if lvl == 0)
            * request.unit_scale_ns
            * 1e-9
        )
        return (v * v / self.sense_ohms) * seconds * 1e6  # J -> uJ

    def check_violations(self, request: ShotRequest) -> list[dict[str, Any]]:
        """静态检查，返回违例明细列表（空列表 = 通过）。"""
        v: list[dict[str, Any]] = []
        if self.tripped_reason is not None:
            v.append({
                "code": "interlock_tripped",
                "message": f"互锁已触发（{self.tripped_reason}），需人工 reset_interlock",
            })
        if self.shots_used >= self.shot_budget:
            v.append({
                "code": "shot_budget_exhausted",
                "message": f"射击预算耗尽: {self.shots_used} >= {self.shot_budget}",
            })
        if request.v_target_mv <= 0 or request.v_target_mv > self.v_target_max_mv:
            v.append({
                "code": "v_target_out_of_range",
                "message": f"v_target_mv={request.v_target_mv} 超出上限 {self.v_target_max_mv}",
            })
        if request.delay_raw < 0:
            v.append({"code": "delay_negative", "message": f"delay_raw={request.delay_raw} 为负"})
        if request.pulse_raw < 0:
            v.append({"code": "pulse_negative", "message": f"pulse_raw={request.pulse_raw} 为负"})
        if request.pulse_raw > self.max_pulse_raw:
            v.append({
                "code": "pulse_raw_exceeds_limit",
                "message": (
                    f"pulse_raw={request.pulse_raw} > 上限 {self.max_pulse_raw}"
                    f"（= {self.max_pulse_raw * self.unit_scale_ns} ns）"
                ),
            })
        if len(request.pattern) > self.pattern_max_segments:
            v.append({
                "code": "pattern_too_many_segments",
                "message": f"pattern {len(request.pattern)} 段 > 上限 {self.pattern_max_segments}",
            })
        for lvl, _dur in request.pattern:
            if lvl not in (0, 1):
                v.append({
                    "code": "pattern_invalid_level",
                    "message": f"pattern level 必须 0/1: {lvl}",
                })
                break
        for _lvl, dur in request.pattern:
            if dur < 0:
                v.append({
                    "code": "pattern_negative_duration",
                    "message": f"pattern duration 为负: {dur}",
                })
                break
        total_ns = sum(d for _, d in request.pattern) * request.unit_scale_ns
        if total_ns > self.pattern_max_total_ns:
            v.append({
                "code": "pattern_too_long",
                "message": f"pattern 总时长 {total_ns}ns > 上限 {self.pattern_max_total_ns}ns",
            })
        energy = self.estimate_energy_uj(request)
        if energy > self.max_energy_uj:
            v.append({
                "code": "energy_exceeds_limit",
                "message": f"估算能量 {energy:.3f}uJ > 上限 {self.max_energy_uj}uJ",
            })
        return v

    def check_shot(self, request: ShotRequest, audit: Any = None) -> None:
        """越界即拒绝并落审计（抛 SafetyViolation；不消耗预算）。"""
        violations = self.check_violations(request)
        if violations:
            if audit is not None:
                audit.record(
                    "hwctl.safety",
                    "safety.shot_reject",
                    "rig",
                    {
                        "delay_raw": request.delay_raw,
                        "pulse_raw": request.pulse_raw,
                        "violations": violations,
                    },
                )
            raise SafetyViolation(
                violations[0]["code"], violations[0]["message"], violations
            )

    # ---------------- 运行时互锁 ----------------

    def consume_shot(self) -> None:
        """记账一次已执行的射击（含失败；对齐 FI finally 计数语义）。"""
        self.shots_used += 1

    def record_outcome(self, outcome: str, audit: Any = None) -> None:
        """更新连发 crash 计数；达上限触发互锁（互锁 = 物理止损，不是超时）。"""
        if outcome == "crash":
            self.consecutive_crash += 1
            if self.consecutive_crash >= self.max_consecutive_crash and self.tripped_reason is None:
                self.tripped_reason = f"consecutive_crash>={self.max_consecutive_crash}"
                if audit is not None:
                    audit.record(
                        "hwctl.safety",
                        "safety.interlock_trip",
                        "rig",
                        {
                            "reason": self.tripped_reason,
                            "consecutive_crash": self.consecutive_crash,
                        },
                    )
        else:
            self.consecutive_crash = 0

    def reset_interlock(self, actor: str, audit: Any = None) -> None:
        """显式解除互锁（人工确认接缝；审计记录操作者）。"""
        self.tripped_reason = None
        self.consecutive_crash = 0
        if audit is not None:
            audit.record(
                "hwctl.safety", "safety.interlock_reset", "rig", {"actor": actor}
            )

    def check_xor_invariants(self, audit: Any = None) -> None:
        """运行前批量复核全部掩码不变量（加载时已逐条校验，此处供显式复查）。"""
        for inv in self.xor_invariants:
            mismatch = inv.mismatch()
            if mismatch is not None:
                if audit is not None:
                    audit.record(
                        "hwctl.safety",
                        "safety.xor_mask_reject",
                        inv.name,
                        {"detail": mismatch},
                    )
                raise SafetyViolation("xor_mask_mismatch", mismatch)
