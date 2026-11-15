# -*- coding: utf-8 -*-
"""安全域测试：越界拒绝 + 审计 + XOR 掩码校验 + 只读加载纪律。"""

import json
from pathlib import Path

import pytest

from hwctl.protocol import ShotRequest, build_glitch_pattern
from hwctl.ledger import AuditLog
from hwctl.safety import (
    DEFAULT_CONFIG_RESOURCE,
    SafetyDomain,
    SafetyViolation,
    validate_xor_mask,
)

BASE_CONFIG = {
    "schema": "hwctl.safety-domain-v0",
    "v_target_max_mv": 3300,
    "max_pulse_raw": 100,
    "pattern_max_segments": 8,
    "pattern_max_total_ns": 2_000_000,
    "sense_ohms": 50.0,
    "max_energy_uj": 500.0,
    "max_consecutive_crash": 3,
    "shot_budget": 5,
    "unit_scale_ns": 10,
}


def write_config(tmp_path, **overrides):
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = dict(BASE_CONFIG)
    cfg.update(overrides)
    path = tmp_path / "safety-domain.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return str(path)


def make_request(pulse_raw=2, delay_raw=100, v_target_mv=3300, pattern=None):
    if pattern is None:
        pattern = build_glitch_pattern(delay_raw, pulse_raw)
    return ShotRequest(
        delay_raw=delay_raw,
        pulse_raw=pulse_raw,
        delay_ns=delay_raw * 10,
        pulse_ns=pulse_raw * 10,
        pattern=tuple(pattern),
        v_target_mv=v_target_mv,
    )


def read_audit(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------- 越界拒绝 ----------------

def test_pulse_over_limit_rejected_and_audited(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    with AuditLog(str(audit_path)) as audit:
        domain = SafetyDomain.from_dict(BASE_CONFIG)
        with pytest.raises(SafetyViolation) as excinfo:
            domain.check_shot(make_request(pulse_raw=101), audit=audit)
    assert excinfo.value.code == "pulse_raw_exceeds_limit"
    actions = [e["action"] for e in read_audit(audit_path)]
    assert "safety.shot_reject" in actions


def test_v_target_out_of_range_rejected(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    with AuditLog(str(audit_path)) as audit:
        domain = SafetyDomain.from_dict(BASE_CONFIG)
        with pytest.raises(SafetyViolation) as excinfo:
            domain.check_shot(make_request(v_target_mv=5000), audit=audit)
    assert excinfo.value.code == "v_target_out_of_range"


def test_pattern_segment_limit_rejected(tmp_path):
    domain = SafetyDomain.from_dict(BASE_CONFIG)
    bad = [(1, 10)] * 9  # 9 段 > 设备硬限 8
    with pytest.raises(SafetyViolation) as excinfo:
        domain.check_shot(make_request(pattern=bad))
    assert excinfo.value.code == "pattern_too_many_segments"


def test_pattern_total_duration_rejected(tmp_path):
    domain = SafetyDomain.from_dict(BASE_CONFIG)  # 上限 2ms
    pattern = build_glitch_pattern(300_000, 2)  # (300000+2+1)*10ns ≈ 3.0ms
    with pytest.raises(SafetyViolation) as excinfo:
        domain.check_shot(make_request(pattern=pattern))
    assert excinfo.value.code == "pattern_too_long"


def test_energy_limit_rejected(tmp_path):
    cfg = dict(BASE_CONFIG, sense_ohms=1.0, max_energy_uj=5.0)
    domain = SafetyDomain.from_dict(cfg)
    # 3.3V^2/1ohm * 1us = 10.89uJ > 5uJ
    with pytest.raises(SafetyViolation) as excinfo:
        domain.check_shot(make_request(pulse_raw=100))
    assert excinfo.value.code == "energy_exceeds_limit"
    # 同参数换 sense=50ohm：0.218uJ，通过
    ok_domain = SafetyDomain.from_dict(dict(BASE_CONFIG, max_energy_uj=5.0))
    ok_domain.check_shot(make_request(pulse_raw=100))


def test_shot_budget_exhausted(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    with AuditLog(str(audit_path)) as audit:
        domain = SafetyDomain.from_dict(BASE_CONFIG)  # shot_budget=5
        req = make_request()
        for _ in range(5):
            domain.check_shot(req, audit=audit)
            domain.consume_shot()
        with pytest.raises(SafetyViolation) as excinfo:
            domain.check_shot(req, audit=audit)
    assert excinfo.value.code == "shot_budget_exhausted"


def test_consecutive_crash_interlock_trip_and_reset(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    with AuditLog(str(audit_path)) as audit:
        domain = SafetyDomain.from_dict(BASE_CONFIG)  # max_consecutive_crash=3
        req = make_request()
        domain.record_outcome("crash", audit=audit)
        domain.record_outcome("crash", audit=audit)
        assert domain.tripped_reason is None
        domain.record_outcome("crash", audit=audit)
        assert domain.tripped_reason is not None
        with pytest.raises(SafetyViolation) as excinfo:
            domain.check_shot(req, audit=audit)
        assert excinfo.value.code == "interlock_tripped"
        # 非 crash 结果清零连发计数
        domain2 = SafetyDomain.from_dict(BASE_CONFIG)
        domain2.record_outcome("crash")
        domain2.record_outcome("crash")
        domain2.record_outcome("no_effect")
        assert domain2.consecutive_crash == 0
        # 人工确认接缝：显式解除 + 审计 actor
        domain.reset_interlock("human-op", audit=audit)
        assert domain.tripped_reason is None
        domain.check_shot(req, audit=audit)
    entries = read_audit(audit_path)
    assert any(e["action"] == "safety.interlock_trip" for e in entries)
    resets = [e for e in entries if e["action"] == "safety.interlock_reset"]
    assert resets and resets[0]["detail"]["actor"] == "human-op"


# ---------------- XOR 掩码校验（FI 教训）----------------

def test_xor_mask_valid_passes():
    # source ^ target = 0b1100 ^ 0b1001 = 0b0101
    validate_xor_mask(0b1100, 0b1001, 0b0101, name="case-ok")


def test_xor_mask_mismatch_rejected_and_audited(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    with AuditLog(str(audit_path)) as audit:
        with pytest.raises(SafetyViolation) as excinfo:
            validate_xor_mask(0b1100, 0b1001, 0b1111, name="case-bad", audit=audit)
    assert excinfo.value.code == "xor_mask_mismatch"
    entries = read_audit(audit_path)
    rejects = [e for e in entries if e["action"] == "safety.xor_mask_reject"]
    assert rejects and rejects[0]["detail"]["expected"] == 0b0101


def test_config_xor_invariant_bad_rejects_load(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    bad_invariant = {
        "name": "reg32", "source": 0xFF00, "target": 0x0FF0, "bit_mask_on_reg_value": 0x00FF,
    }
    assert 0xFF00 ^ 0x0FF0 == 0xF0F0  # 正确掩码应为 0xF0F0，声明 0x00FF 即错
    cfg = dict(BASE_CONFIG, xor_invariants=[bad_invariant])
    path = write_config(tmp_path / "x", **{"xor_invariants": [bad_invariant]})
    with AuditLog(str(audit_path)) as audit:
        with pytest.raises(SafetyViolation) as excinfo:
            SafetyDomain.from_json_file(path, audit=audit)
    assert excinfo.value.code == "xor_mask_mismatch"
    actions = [e["action"] for e in read_audit(audit_path)]
    assert "safety.config_reject" in actions


def test_config_xor_invariant_good_loads(tmp_path):
    good = {"name": "reg32", "source": 0xFF00, "target": 0x0FF0, "bit_mask_on_reg_value": 0xF0F0}
    path = write_config(tmp_path, xor_invariants=[good])
    domain = SafetyDomain.from_json_file(path)
    assert len(domain.xor_invariants) == 1
    domain.check_xor_invariants()  # 显式复查通过


def test_config_missing_required_key_rejected(tmp_path):
    cfg = {k: v for k, v in BASE_CONFIG.items() if k != "sense_ohms"}
    with pytest.raises(SafetyViolation) as excinfo:
        SafetyDomain.from_dict(cfg)
    assert excinfo.value.code == "config_missing_keys"


def test_default_resource_loads_and_invariants_consistent():
    domain = SafetyDomain.from_json_file(DEFAULT_CONFIG_RESOURCE)
    assert domain.v_target_max_mv == 3300
    assert domain.xor_invariants, "随包缺省档应含示例不变量"
    for inv in domain.xor_invariants:
        assert inv.bit_mask_on_reg_value == inv.source ^ inv.target


# ---------------- 条令：配置只读 ----------------

def test_safety_domain_has_no_write_api():
    """条令约定：安全域配置对 Agent 只读——类上不存在任何保存/写回接口。"""
    forbidden = [
        m for m in dir(SafetyDomain)
        if any(word in m.lower() for word in ("save", "write_back", "dump", "store"))
    ]
    assert forbidden == []


def test_runtime_state_not_part_of_config_file(tmp_path):
    path = write_config(tmp_path)
    domain = SafetyDomain.from_json_file(path)
    domain.consume_shot()
    domain.record_outcome("crash")
    # 重载即复位（运行时状态不落配置文件）
    domain2 = SafetyDomain.from_json_file(path)
    assert domain2.shots_used == 0
    assert domain2.consecutive_crash == 0
