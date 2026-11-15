# -*- coding: utf-8 -*-
"""Mock 全链 dry-run 测试：50 点 uniform 扫描出 CSV + 复位分档 + 安全拒绝 + CLI。"""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hwctl.drivers import (
    MockPico3000Scope,
    MockPowerShorterDevice,
    MockSmartHub,
    MockUartGuarderDevice,
    Pico3000Scope,
    PowerShorterRig,
    SmartHubPwr,
    UartGuarderTrigger,
)
from hwctl.ledger import AuditLog, ShotLedger
from hwctl.protocol import PwrDriver, RigDriver, ScopeDriver, TriggerDriver
from hwctl.safety import SafetyDomain, SafetyViolation
from hwctl.scan.executor import TIMINGS_DRYRUN, ShotExecutor
from hwctl.scan.judge import OUTCOMES, MockShotJudge, ScriptedScenario
from hwctl.scan.params import HyperParams, ParamAxis, ParamWindow, window_from_grid
from hwctl.scan.strategy import UniformStrategy

REPO_ROOT = Path(__file__).resolve().parents[1]

GRID_DICT = {
    "axes": [
        {"name": "delay", "unit": "raw", "min": 0, "max": 900, "step": 100},
        {"name": "pulse", "unit": "raw", "min": 0, "max": 8, "step": 2},
    ],
    "strategy": "uniform",
    "campaign_id": "demo-001",
}


def build_stack(tmp_path, audit=None, safety=None):
    """Mock 全链组装（与 cli.cmd_scan 同构；测试直接驱动，不依赖子进程）。"""
    audit = audit if audit is not None else AuditLog(str(tmp_path / "audit.jsonl"))
    safety = safety if safety is not None else SafetyDomain.load_default(None)
    mock_ps = MockPowerShorterDevice()
    rig = PowerShorterRig(mock_ps, audit=audit)
    mock_ug = MockUartGuarderDevice(golden_response=b"OK", rtt_ms=5.0)
    trigger = UartGuarderTrigger(mock_ug, audit=audit)
    trigger.configure(match_bytes=bytes.fromhex("AABBCC"), baud=115200)
    mock_shub = MockSmartHub()
    pwr = SmartHubPwr(mock_shub, audit=audit)
    pwr.set_power(1, True)
    return audit, safety, mock_ps, rig, mock_ug, trigger, mock_shub, pwr


def run_scan(tmp_path, audit, safety, rig, trigger, mock_ug, mock_shub, pwr):
    window, meta = window_from_grid(GRID_DICT)
    ledger = ShotLedger(str(tmp_path / "ledger.csv"), audit=audit)
    try:
        scenario = ScriptedScenario(window, uart=mock_ug, pwr=mock_shub)
        judge = MockShotJudge(uart=mock_ug, pwr=pwr, prepare=scenario.prepare)
        executor = ShotExecutor(
            rig, trigger, judge, ledger, safety, audit,
            pwr=pwr, timings=TIMINGS_DRYRUN,
            campaign_id=meta["campaign_id"], strategy_name="uniform",
        )
        plan = UniformStrategy(hyper=HyperParams()).plan(window, shot_budget=safety.shot_budget)
        records = executor.run(plan)
        return executor, ledger, records
    finally:
        ledger.close()


def read_csv_rows(path):
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    header, data = rows[0], rows[1:]
    return header, [dict(zip(header, r)) for r in data]


# ---------------- 50 点 uniform 全链 dry-run ----------------

def test_mock_full_chain_50_points_uniform_csv(tmp_path):
    audit, safety, mock_ps, rig, mock_ug, trigger, mock_shub, pwr = build_stack(tmp_path)
    executor, ledger, records = run_scan(
        tmp_path, audit, safety, rig, trigger, mock_ug, mock_shub, pwr
    )
    # 50 点全部出记录
    assert len(records) == 50
    assert all(r.outcome in OUTCOMES for r in records)
    # 确定性场景：success 带 1 点（delay=400, pulse=2）；crash 平台 10 点（pulse=8）；其余 no_effect
    counts = {}
    for r in records:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    assert counts == {"success": 1, "crash": 10, "no_effect": 39}
    # 判定走的是 Mock 信号链：success 点为错字符应答，crash 点有 boot 重试与电流掉底
    succ = next(r for r in records if r.outcome == "success")
    assert succ.verdict.channels["C2"]["resp_diff_form"] == "wrong_chars"
    assert succ.verdict.heat_score == 100.0  # 无基线时 z=0 -> heat = 100*success
    crash = next(r for r in records if r.outcome == "crash")
    assert crash.verdict.channels["C5"]["boot_retry_count"] >= 1
    assert crash.verdict.channels["C3"]["valley_ma"] is not None
    assert mock_ps.last_shot is not None  # rig mock 记录了最后一射

    # CSV：50 行数据 + 列名对齐
    header, rows = read_csv_rows(tmp_path / "ledger.csv")
    assert len(rows) == 50
    for col in (
        "shot_id", "timestamp", "delay_raw", "pulse_raw", "delay_ns", "pulse_ns",
        "outcome", "state_label", "responded", "unit_scale", "unit_source",
        "pattern_json", "resp_latency_ms", "resp_diff_form", "resp_diff_code",
        "valley_ma", "valley_offset_ms", "boot_retry_count", "heat_score",
        "reset_mode", "error",
    ):
        assert col in header, f"缺列: {col}"
    by_id = {int(r["shot_id"]): r for r in rows}
    assert sorted(by_id) == list(range(1, 51))
    row = by_id[succ.shot_id]
    assert row["outcome"] == "success"
    assert row["resp_diff_form"] == "wrong_chars"
    assert row["resp_diff_code"] == "3"
    assert row["unit_scale"] == "10"
    assert "10ns" in row["unit_source"]
    d_raw = int(float(row["delay_raw"]))
    p_raw = int(float(row["pulse_raw"]))
    assert json.loads(row["pattern_json"]) == [[1, d_raw], [0, p_raw], [1, 1]]
    assert row["responded"] == "true"

    # 台账已 durable（run 结束 = 批尾 flush+fsync）
    assert ledger.durable_shot_id() == 50

    # 审计：可重放一次实验会话（open/configure/arm/inject/reset/close）
    entries = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l]
    actions = {e["action"] for e in entries}
    assert {"ledger.open", "ledger.close", "rig.configure", "rig.arm",
            "rig.inject", "rig.reset_target", "scan.shot"} <= actions
    resets = [e for e in entries if e["action"] == "rig.reset_target"]
    assert any(e["detail"]["mode"] == "power_cycle" for e in resets)


def test_protocol_conformance_of_driver_stack(tmp_path):
    """四个驱动实现满足 runtime_checkable Protocol。"""
    audit, safety, mock_ps, rig, mock_ug, trigger, mock_shub, pwr = build_stack(tmp_path)
    scope = Pico3000Scope(MockPico3000Scope())
    assert isinstance(rig, RigDriver)
    assert isinstance(pwr, PwrDriver)
    assert isinstance(scope, ScopeDriver)
    assert isinstance(trigger, TriggerDriver)


def test_reset_tiering_power_cycle_only_after_crash(tmp_path):
    """复位分档：常规射 RESET 脉冲；仅上一射 crash 才 0V 断电。"""
    audit, safety, mock_ps, rig, mock_ug, trigger, mock_shub, pwr = build_stack(tmp_path)
    _executor, _ledger, records = run_scan(
        tmp_path, audit, safety, rig, trigger, mock_ug, mock_shub, pwr
    )
    assert records[0].reset_mode == "reset_pulse"  # 首射常规档
    for prev, cur in zip(records, records[1:]):
        expected = "power_cycle" if prev.outcome == "crash" else "reset_pulse"
        assert cur.reset_mode == expected, f"prev={prev.outcome} cur.reset_mode={cur.reset_mode}"
    # 至少发生过一次 power_cycle（crash 有 10 发）且接在 crash 后
    assert any(r.reset_mode == "power_cycle" for r in records[1:])


def test_safety_reject_keeps_budget_and_scan_alive(tmp_path):
    """越界点：拒绝记账、不消耗预算、扫描继续、审计落 safety.shot_reject。"""
    cfg_path = tmp_path / "tight-safety.json"
    cfg = {
        "schema": "hwctl.safety-domain-v0",
        "v_target_max_mv": 3300,
        "max_pulse_raw": 3,               # grid pulse 0..8 -> pulse>=4 全拒
        "pattern_max_segments": 8,
        "pattern_max_total_ns": 2_000_000,
        "sense_ohms": 50.0,
        "max_energy_uj": 500.0,
        "max_consecutive_crash": 20,
        "shot_budget": 10_000,
        "unit_scale_ns": 10,
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    safety = SafetyDomain.from_json_file(str(cfg_path), audit=audit)
    _a, _s, mock_ps, rig, mock_ug, trigger, mock_shub, pwr = build_stack(
        tmp_path, audit=audit, safety=safety
    )
    _executor, _ledger, records = run_scan(
        tmp_path, audit, safety, rig, trigger, mock_ug, mock_shub, pwr
    )
    rejected = [r for r in records if r.outcome == "rejected"]
    executed = [r for r in records if r.outcome != "rejected"]
    assert len(records) == 50
    assert len(rejected) == 30           # pulse ∈ {4,6,8} x 10 delay
    assert len(executed) == 20           # pulse ∈ {0,2}
    assert safety.shots_used == 20       # 拒绝射不消耗预算
    assert all("pulse_raw_exceeds_limit" in (r.row.get("error") or "") for r in rejected)
    assert all(r.reset_mode is None for r in rejected)
    entries = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l]
    assert any(e["action"] == "safety.shot_reject" for e in entries)


def test_ensure_flushed_fact_ordering_interface(tmp_path):
    """fact 落图铁律接口：ensure_flushed(shot_id) 返回即该行已 fsync。"""
    audit, safety, mock_ps, rig, mock_ug, trigger, mock_shub, pwr = build_stack(tmp_path)
    executor, ledger, records = run_scan(
        tmp_path, audit, safety, rig, trigger, mock_ug, mock_shub, pwr
    )
    last = records[-1]
    executor.ensure_flushed(last.shot_id)  # sapper 落 fact 前调用
    assert ledger.durable_shot_id() >= last.shot_id
    _header, rows = read_csv_rows(tmp_path / "ledger.csv")
    assert int(rows[-1]["shot_id"]) >= last.shot_id


def test_run_in_cycle_is_v1_interface(tmp_path):
    audit, safety, mock_ps, rig, mock_ug, trigger, mock_shub, pwr = build_stack(tmp_path)
    window, _meta = window_from_grid(GRID_DICT)
    ledger = ShotLedger(str(tmp_path / "l.csv"))
    executor = ShotExecutor(rig, trigger, MockShotJudge(), ledger, safety, audit,
                            timings=TIMINGS_DRYRUN)
    plan = UniformStrategy().plan(window)
    with pytest.raises(NotImplementedError):
        executor.run_in_cycle(plan)
    ledger.close()


# ---------------- CLI：python -m hwctl ----------------

def _write_grid(tmp_path):
    grid = tmp_path / "grid.json"
    grid.write_text(json.dumps(GRID_DICT), encoding="utf-8")
    return grid


def test_cli_scan_dry_run_end_to_end(tmp_path):
    grid = _write_grid(tmp_path)
    out = tmp_path / "out" / "ledger.csv"
    audit_path = tmp_path / "out" / "audit.jsonl"
    proc = subprocess.run(
        [sys.executable, "-m", "hwctl", "scan", "--grid", str(grid), "--dry-run",
         "--out", str(out), "--audit", str(audit_path), "--campaign", "cli-test"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["strategy"] == "uniform"
    assert summary["executed"] == 50
    assert summary["outcomes"] == {"success": 1, "crash": 10, "no_effect": 39}
    assert summary["shot_budget_used"] == 50
    assert summary["ledger_durable_shot_id"] == 50
    _header, rows = read_csv_rows(out)
    assert len(rows) == 50
    # C4 抽样射存证：idx 0/16/32/48 -> 4 个 scope csv
    scope_files = list(out.parent.glob("shot-*-scope.csv"))
    assert len(scope_files) == 4
    audit_entries = [json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l]
    assert any(e["action"] == "scan.end" for e in audit_entries)
    assert any(e["action"] == "scope.download" for e in audit_entries)


def test_cli_devices_returns_json_list(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "hwctl", "devices"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    devices = json.loads(proc.stdout)
    assert isinstance(devices, list)  # 无设备环境 -> 空列表（本机未装 OSR 包）


def test_cli_refuses_real_mode_on_c1_skeleton(tmp_path):
    grid = _write_grid(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "hwctl", "scan", "--grid", str(grid)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "dry-run" in proc.stderr


def test_cli_rejects_out_of_range_batch_size_hint(tmp_path):
    grid = _write_grid(tmp_path)
    out = tmp_path / "l.csv"
    proc = subprocess.run(
        [sys.executable, "-m", "hwctl", "scan", "--grid", str(grid), "--dry-run",
         "--batch-size", "999", "--out", str(out),
         "--audit", str(tmp_path / "a.jsonl")],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "超参越界" in proc.stderr
