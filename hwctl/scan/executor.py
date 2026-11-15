# -*- coding: utf-8 -*-
"""执行器：单射状态机（scan-acceleration.md §3.2 / §6.2；tool-integration.md §3）。

单射固定序（"arm 先行、触发在后"三原则）：
  0. 安全域检查：越界即拒绝并落审计（不消耗设备、不消耗预算）；
  1. 复位分档：上一射 crash -> 0V 断电（power_cycle，~300ms）；
     否则 RESET 脉冲（reset_pulse，常规射）；
  2. 预 arm 流水：judge.before_shot -> scope.arm(抽样射) -> rig.configure
     -> rig.arm -> trigger.arm -> state=='armed' 确认；
  3. 触发：MVP 软触发（rig.soft_trigger）；硬件触发链（UartGuarder TRI ->
     trigger_edges 同源闭环）v1 接入 TODO；
  4. 判定：judge 通道向量（C1/C2/C3[+C5]；C4 抽样射只存证不提特征）；
  5. 记账：台账异步 append；批尾 flush_all（批内攒写 + fsync）；
     ensure_flushed(shot_id) 为结果 fact 落图铁律接口；
  6. 安全域消费：预算计数 + 连发 crash 互锁（tripped 即停批并落审计）。

in-cycle 多射（一次 boot 多射，trigger_edges=N，§12.1 杠杆 1）留接口 run_in_cycle。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from hwctl.ledger import ShotLedger
from hwctl.protocol import (
    UNIT_SOURCE,
    InjectionReceipt,
    build_glitch_pattern,
    request_from_point,
    utc_now_iso,
)
from hwctl.safety import SafetyDomain, SafetyViolation
from hwctl.scan.judge import ShotJudge, Verdict
from hwctl.scan.params import ParamPoint
from hwctl.scan.strategy import ScanPlan


@dataclass
class Timings:
    """单射时序参数（§3.2 量级；dry-run/Mock 传 0 缩放，实机待校准）。"""

    reset_pulse_s: float = 0.02        # RESET 脉冲保持（设备规格量级 10-50ms）
    power_cycle_hold_s: float = 0.3    # 0V 断电保持（OSR-2560 手册复位法 300ms）
    boot_settle_s: float = 0.0         # 复位后 boot 等待（冷复位 50-150ms 量级；in-cycle 为 0）


#: dry-run / Mock / CI：零时序（链路等价，不睡真时间）
TIMINGS_DRYRUN = Timings(reset_pulse_s=0.0, power_cycle_hold_s=0.0, boot_settle_s=0.0)
#: 实机缺省（§3.2 量级；实机校准 TODO P0）
TIMINGS_REAL = Timings()


@dataclass
class ShotRecord:
    """一射的完整记录（台账行 + 判定向量 + 回执引用）。"""

    shot_id: int | None
    point: ParamPoint
    outcome: str
    reset_mode: str | None
    receipt: InjectionReceipt | None
    verdict: Verdict | None
    row: dict[str, Any] = field(default_factory=dict)


class ShotExecutor:
    """单射状态机 + 批驱动（参数序列来自 ScanPlan；台账/安全域/审计都在 hwctl 层）。"""

    def __init__(
        self,
        rig: Any,
        trigger: Any,
        judge: ShotJudge,
        ledger: ShotLedger,
        safety: SafetyDomain,
        audit: Any,
        *,
        pwr: Any = None,
        scope: Any = None,
        timings: Timings | None = None,
        campaign_id: str = "unknown",
        strategy_name: str = "uniform",
        scope_sample_every: int = 16,
        scope_dir: str | None = None,
        actor: str = "hwctl.executor",
    ) -> None:
        self.rig = rig
        self.trigger = trigger
        self.judge = judge
        self.ledger = ledger
        self.safety = safety
        self.audit = audit
        self.pwr = pwr
        self.scope = scope
        self.timings = timings if timings is not None else TIMINGS_DRYRUN
        self.campaign_id = campaign_id
        self.strategy_name = strategy_name
        self.scope_sample_every = max(1, int(scope_sample_every))
        self.scope_dir = scope_dir
        self.actor = actor
        self._last_outcome: str | None = None

    # ---------------- 批驱动 ----------------

    def run(self, plan: ScanPlan) -> list[ShotRecord]:
        """按参数序列逐射；批尾 flush+fsync；互锁触发即停批（落审计）。"""
        records: list[ShotRecord] = []
        outcomes: list[str] = []
        for idx, point in enumerate(plan.points):
            if self.safety.tripped_reason is not None:
                self._audit("scan.stop_interlock", "hwctl", {
                    "reason": self.safety.tripped_reason,
                    "remaining_points": len(plan.points) - idx,
                })
                break
            if plan.stop(len(records), outcomes):
                self._audit("scan.stop_predicate", "hwctl", {"shots_done": len(records)})
                break
            record = self.run_single(
                point, shot_index=idx, batch_id=idx // max(1, plan.batch_size)
            )
            records.append(record)
            outcomes.append(record.outcome)
            if (idx + 1) % max(1, plan.batch_size) == 0:
                self.ledger.flush_all()  # 批尾 flush + fsync（§3.2 #8）
        self.ledger.flush_all()
        return records

    # ---------------- 单射状态机 ----------------

    def run_single(self, point: ParamPoint, *, shot_index: int, batch_id: int = 0) -> ShotRecord:
        row: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "campaign_id": self.campaign_id,
            "shot_index": shot_index,
            "strategy": self.strategy_name,
            "batch_id": batch_id,
            "unit_scale": self.rig.unit_scale_ns,
            "unit_source": UNIT_SOURCE,
            "reset_mode": None,
        }

        # ---- 0. 设备级参数 + 安全域检查（越界即拒绝并落审计）----
        try:
            request = request_from_point(
                point,
                unit_scale_ns=self.rig.unit_scale_ns,
                v_target_mv=self.rig.v_target_mv,
            )
        except ValueError as exc:
            return self._finish_rejected(row, point, "param_mapping_failed", str(exc))
        try:
            self.safety.check_shot(request, audit=self.audit)
        except SafetyViolation as sv:
            return self._finish_rejected(row, point, sv.code, sv.message)

        # ---- 1. 复位分档：常规射 RESET 脉冲；仅 crash 才 0V 断电 ----
        reset_mode = "power_cycle" if self._last_outcome == "crash" else "reset_pulse"
        hold = (
            self.timings.power_cycle_hold_s
            if reset_mode == "power_cycle"
            else self.timings.reset_pulse_s
        )
        self.rig.reset_target(mode=reset_mode, hold_s=hold)
        row["reset_mode"] = reset_mode
        if self.timings.boot_settle_s > 0:
            time.sleep(self.timings.boot_settle_s)

        # ---- 2. 预 arm 流水 ----
        self.judge.before_shot(point)
        sampled = (shot_index % self.scope_sample_every == 0) and self.scope is not None
        if sampled:
            self.scope.arm()
        pattern = build_glitch_pattern(request.delay_raw, request.pulse_raw)
        try:
            self.rig.configure("voltage", pattern, trigger_edges=1, pattern_repeat=1)
            self.rig.arm()
            arm_state = self.rig.state()
            if arm_state != "armed":
                return self._finish_other(row, point, f"rig_state={arm_state}")
            self.trigger.arm()
            trig_state = self.trigger.state()
            if trig_state not in ("armed", "triggered"):
                return self._finish_other(row, point, f"trigger_state={trig_state}")
        except SafetyViolation as sv:
            return self._finish_other(row, point, f"rig_safety:{sv.code}", sv.message)
        except Exception as exc:  # noqa: BLE001 —— 设备异常要记账而非中断扫描
            return self._finish_other(row, point, "device_error", f"{type(exc).__name__}: {exc}")

        # ---- 3. 触发（MVP 软触发；硬件 TRI 触发链 v1 TODO）----
        if self.pwr is not None:
            mark = getattr(self.pwr, "mark_trigger", None)
            if mark is not None:
                mark()
        receipt = self.rig.soft_trigger()
        if not receipt.ok:
            return self._finish_other(row, point, "soft_trigger_failed", receipt.error or "")

        # ---- 4. 判定（通道向量）----
        verdict = self.judge.judge(point, receipt, reset_mode=reset_mode)

        # ---- 4b. C4 抽样射：scope 存证（特征提取 v1 TODO）----
        if sampled:
            self._sample_scope(shot_index)

        # ---- 5. 记账（异步；批尾由 run() flush）----
        row.update({
            "delay_raw": receipt.delay_raw,
            "pulse_raw": receipt.pulse_raw,
            "delay_ns": receipt.delay_ns,
            "pulse_ns": receipt.pulse_ns,
            "outcome": verdict.outcome,
            "state_label": verdict.state_label,
            "responded": verdict.responded,
            "pattern_json": json.dumps(receipt.pattern, separators=(",", ":")),
            "resp_latency_ms": verdict.channels.get("C1", {}).get("resp_latency_ms"),
            "resp_diff_form": verdict.channels.get("C2", {}).get("resp_diff_form"),
            "resp_diff_code": verdict.channels.get("C2", {}).get("resp_diff_code"),
            "valley_ma": verdict.channels.get("C3", {}).get("valley_ma"),
            "valley_offset_ms": verdict.channels.get("C3", {}).get("valley_offset_ms"),
            "sag_depth_mv": verdict.channels.get("C4", {}).get("sag_depth_mv"),
            "sag_duration_us": verdict.channels.get("C4", {}).get("sag_duration_us"),
            "rail_recovery_ms": verdict.channels.get("C4", {}).get("rail_recovery_ms"),
            "boot_retry_count": verdict.channels.get("C5", {}).get("boot_retry_count"),
            "heat_score": verdict.heat_score,
        })
        shot_id = self.ledger.append(row)

        # ---- 6. 安全域消费（预算 + 连发 crash 互锁）----
        self.safety.consume_shot()
        self.safety.record_outcome(verdict.outcome, audit=self.audit)
        self._last_outcome = verdict.outcome
        self._audit("scan.shot", "hwctl", {
            "shot_id": shot_id,
            "delay_raw": receipt.delay_raw,
            "pulse_raw": receipt.pulse_raw,
            "outcome": verdict.outcome,
            "reset_mode": reset_mode,
            "interlock_tripped": self.safety.tripped_reason,
        })
        return ShotRecord(
            shot_id=shot_id,
            point=point,
            outcome=verdict.outcome,
            reset_mode=reset_mode,
            receipt=receipt,
            verdict=verdict,
            row=row,
        )

    # ---------------- fact 落图铁律接口 ----------------

    def ensure_flushed(self, shot_id: int) -> None:
        """结果 fact 落图前必须调用：该 shot_id 行已 flush + fsync 落盘。

        铁律：记录先于相信（scan-acceleration.md §3.2 #8）——sapper 侧
        _dispatch_scan 落 fact 前先走这里，台账不 durable 不落图。
        """
        self.ledger.flush_before(shot_id)

    # ---------------- in-cycle 多射（v1 接口）----------------

    def run_in_cycle(
        self,
        plan: ScanPlan,
        *,
        max_shots_per_cycle: int = 50,
        verify_per_shot: bool = True,
    ) -> list[ShotRecord]:
        """in-cycle 多射（一次 boot 多射，trigger_edges=N）——v1 接口。

        §12.1 杠杆 1：单射周期 2.0-2.7s -> 0.15-0.35s。护栏（缺一不可）：
        - 每射金标应答校验（防"未 crash 但已损坏"的假 no_effect）；
        - in-cycle 连射封顶 max_shots_per_cycle（缺省 50）；
        - 校验失败立即冷复位（0V 断电档）再恢复；
        - E5 专项验收：误损注入点触发完整性校验 -> 冷复位 -> 恢复，全程无假 no_effect。
        """
        raise NotImplementedError(
            "in-cycle 多射为 v1 落点（fork-plan §12.1 杠杆 1 / E5 专项）；"
            "MVP 用冷复位单射（run）。"
        )

    # ---------------- 内部 ----------------

    def _finish_rejected(
        self, row: dict[str, Any], point: ParamPoint, code: str, message: str
    ) -> ShotRecord:
        """安全域拒绝：记账（不消耗设备与预算），扫描继续。"""
        row["outcome"] = "rejected"
        row["error"] = f"{code}: {message}"
        shot_id = self.ledger.append(row)
        self._audit("scan.shot_rejected", "hwctl", {
            "shot_id": shot_id, "code": code, "point": dict(point.values),
        })
        return ShotRecord(shot_id=shot_id, point=point, outcome="rejected",
                          reset_mode=None, receipt=None, verdict=None, row=row)

    def _finish_other(
        self, row: dict[str, Any], point: ParamPoint, code: str, message: str = ""
    ) -> ShotRecord:
        """设备/状态异常：记账 outcome=other，扫描继续（射未发生，不 consume）。"""
        row["outcome"] = "other"
        row["error"] = f"{code}: {message}" if message else code
        shot_id = self.ledger.append(row)
        self._audit("scan.shot_error", "hwctl", {
            "shot_id": shot_id, "code": code, "detail": message,
        })
        return ShotRecord(shot_id=shot_id, point=point, outcome="other",
                          reset_mode=row.get("reset_mode"), receipt=None,
                          verdict=None, row=row)

    def _sample_scope(self, shot_index: int) -> None:
        """C4 抽样射：capture + download 存证（波形特征提取 v1 TODO）。"""
        try:
            self.scope.capture()
            if self.scope_dir:
                path = os.path.join(self.scope_dir, f"shot-{shot_index:05d}-scope.csv")
                self.scope.download(path, fmt="csv")
        except Exception as exc:  # noqa: BLE001 —— 采集失败不阻断扫描，落审计
            self._audit("scope.sample_error", "pico3000", {
                "shot_index": shot_index, "error": f"{type(exc).__name__}: {exc}",
            })

    def _audit(self, action: str, device: str, detail: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.record(self.actor, action, device, detail)
