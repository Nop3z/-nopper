# -*- coding: utf-8 -*-
"""策略测试：uniform 网格正确性 + 超参白名单 + adaptive3 接口存在性。"""

import json

import pytest

from hwctl.scan.params import (
    BASE_CHANNEL_WEIGHTS,
    HyperParams,
    ParamAxis,
    ParamPoint,
    ParamWindow,
    param_key,
    window_from_grid,
)
from hwctl.scan.strategy import Adaptive3Strategy, UniformStrategy


def make_window():
    """10 x 5 = 50 点：delay 0..900 step 100；pulse 0..8 step 2（raw 刻度）。"""
    return ParamWindow((
        ParamAxis("delay", "raw", 0, 900, 100),
        ParamAxis("pulse", "raw", 0, 8, 2),
    ))


# ---------------- uniform 网格正确性 ----------------

def test_uniform_grid_50_points_correct():
    window = make_window()
    points = list(window.grid_points())
    assert len(points) == 50
    keys = {p.try_get("delay", "raw") for p in points}, {p.try_get("pulse", "raw") for p in points}
    assert keys[0] == {float(v) for v in range(0, 901, 100)}
    assert keys[1] == {0.0, 2.0, 4.0, 6.0, 8.0}
    assert all(window.contains(p) for p in points)
    assert len({tuple(sorted(p.values.items())) for p in points}) == 50  # 无重复
    # 端点齐全
    pairs = {(p.try_get("delay", "raw"), p.try_get("pulse", "raw")) for p in points}
    assert (0.0, 0.0) in pairs and (900.0, 8.0) in pairs


def test_uniform_grid_order_deterministic_first_axis_outermost():
    points = list(make_window().grid_points())
    # 首轴（delay）最外层：前 5 点 delay 固定为 0，pulse 依次推进
    for i in range(5):
        assert points[i].try_get("delay", "raw") == 0.0
        assert points[i].try_get("pulse", "raw") == 0.0 + 2.0 * i
    assert points[5].try_get("delay", "raw") == 100.0


def test_uniform_plan_shape_and_stop(tmp_path):
    strategy = UniformStrategy()
    plan = strategy.plan(make_window())
    assert plan.strategy == "uniform"
    assert len(plan.points) == 50
    assert plan.batch_size == 50  # 批不超过总点数（min(hyper.batch_size=64, n=50)）
    assert plan.meta["grid_axes"] == ["delay_raw", "pulse_raw"]
    assert plan.stop(49, ["no_effect"] * 49) is False
    assert plan.stop(50, ["no_effect"] * 50) is True


def test_uniform_plan_respects_shot_budget():
    plan = UniformStrategy().plan(make_window(), shot_budget=7)
    assert len(plan.points) == 7  # 预算裁剪，与 safety.shot_budget 对齐


def test_grid_needs_step_for_expansion():
    window = ParamWindow((ParamAxis("delay", "ns", 0, 100, None),))
    with pytest.raises(ValueError):
        list(window.grid_points())


def test_param_key_convention():
    assert param_key("delay", "ns") == "delay_ns"
    assert param_key("pulse", "raw") == "pulse_raw"
    point = ParamPoint({"delay_ns": 31600, "pulse_raw": 2})
    assert point.get("delay", "ns") == 31600
    assert point.try_get("pulse", "ns") is None  # {axis}_{unit} 泛化键


def test_window_from_grid_parses():
    window, meta = window_from_grid({
        "axes": [
            {"name": "delay", "unit": "raw", "min": 0, "max": 900, "step": 100},
            {"name": "pulse", "unit": "raw", "min": 0, "max": 8, "step": 2},
        ],
        "strategy": "uniform",
        "campaign_id": "demo-001",
    })
    assert [a.key for a in window.axes] == ["delay_raw", "pulse_raw"]
    assert meta == {"strategy": "uniform", "campaign_id": "demo-001"}


# ---------------- 超参白名单（§2.5）----------------

def test_hyperparams_defaults_valid():
    HyperParams().validate()


def test_hyperparams_whitelist_rejects_each_bound():
    cases = [
        {"batch_size": 300}, {"batch_size": 10},
        {"step_shrink": 1}, {"step_shrink": 20},
        {"theta_hot": 0.4}, {"theta_hot": 0.97},
        {"epsilon": 0.01}, {"epsilon": 0.5},
        {"confirm_k": 1}, {"confirm_k": 9},
        {"channel_weights": {"C1": 30}},   # > 2x 基准 10
    ]
    for hint in cases:
        with pytest.raises(ValueError) as excinfo:
            HyperParams.from_dict(hint)
        assert "超参越界" in str(excinfo.value)


def test_hyperparams_unknown_key_rejected():
    with pytest.raises(ValueError) as excinfo:
        HyperParams.from_dict({"learning_rate": 0.1})
    assert "未知超参键" in str(excinfo.value)


def test_hyperparams_hint_applied_and_audited(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    from hwctl.ledger import AuditLog
    with AuditLog(str(audit_path)) as audit:
        hyper = HyperParams.from_dict({"batch_size": 32}, audit=audit, actor="agent-hint")
    assert hyper.batch_size == 32
    entries = [json.loads(l) for l in audit_path.read_text(encoding="utf-8").splitlines() if l]
    assert any(e["action"] == "strategy.hint_applied" and e["actor"] == "agent-hint" for e in entries)


def test_channel_weights_renormalize_missing_channel():
    hyper = HyperParams.from_dict({"channel_weights": {"C4": 0.0}})  # C4 关闭
    assert hyper.channel_weights["C4"] == 0.0
    assert hyper.channel_weights["C1"] == BASE_CHANNEL_WEIGHTS["C1"]  # 其余保留基准


# ---------------- adaptive3 三阶段骨架（接口存在性）----------------

def test_adaptive3_interface_exists():
    s = Adaptive3Strategy()
    assert s.name == "adaptive3"
    for meth in ("plan", "plan_stage1_coarse", "plan_stage2_refine", "plan_stage3_confirm"):
        assert callable(getattr(s, meth, None)), f"缺接口: {meth}"


def test_adaptive3_stage2_stage3_not_implemented():
    s = Adaptive3Strategy()
    with pytest.raises(NotImplementedError):
        s.plan_stage2_refine(make_window(), [])
    with pytest.raises(NotImplementedError):
        s.plan_stage3_confirm(make_window(), [])


def test_adaptive3_docstrings_document_stages():
    s = Adaptive3Strategy()
    assert "热区" in Adaptive3Strategy.plan_stage2_refine.__doc__
    assert "二分" in Adaptive3Strategy.plan_stage3_confirm.__doc__


def test_adaptive3_plan_runs_stage1_and_notes_skeleton():
    plan = Adaptive3Strategy().plan(make_window())
    assert len(plan.points) == 50          # stage1 = 均匀粗扫
    assert "stage1" in plan.meta["adaptive3"]
