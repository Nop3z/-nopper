# -*- coding: utf-8 -*-
"""参数值对象 + 策略超参白名单（scan-acceleration.md §2.5）。

ParamAxis / ParamWindow / ParamPoint 泛化自 fi/loop/types.py（{axis}_{unit}
键名规范：delay_ns / pulse_raw / param_key(axis, unit)），闭环层不做任何
隐式单位换算——raw->ns 换算必须经 unit_scale 显式给出并落台账。

超参白名单（模型 Agent 可调全集，§2.5）——**任何超参以 hint 下发，范围校验
后生效并落审计；射击参数坐标本身仍全部由策略器代码生成。**

| 超参            | 范围      | 缺省 |
|-----------------|-----------|------|
| batch_size B    | 16-256    | 64   |
| step_shrink     | 2-16      | 8    |
| theta_hot 偏移  | P50-P95   | P75  |
| epsilon         | 5-20%     | 10%  |
| channel_weights | 0-2x 基准 | 基准 |
| 确认发数 k      | 2-5       | 3    |
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence


def param_key(axis: str, unit: str) -> str:
    """参数键名规范：{axis}_{unit}，如 delay_ns / pulse_raw。"""
    return f"{axis}_{unit}"


@dataclass(frozen=True)
class ParamAxis:
    """一条参数轴。name：delay / pulse（泛化允许其他）；unit：ns / raw。"""

    name: str
    unit: str
    min_value: float
    max_value: float
    step_value: float | None = None   # None = 连续窗口（网格策略不可展开）

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ParamAxis.name 不能为空")
        if self.max_value < self.min_value:
            raise ValueError(f"ParamAxis {self.name}: max < min")
        if self.step_value is not None and self.step_value <= 0:
            raise ValueError(f"ParamAxis {self.name}: step 必须 > 0")

    @property
    def key(self) -> str:
        return param_key(self.name, self.unit)


@dataclass(frozen=True)
class ParamWindow:
    """参数窗口（扫描输入）。axes 至少一条；网格展开顺序 = 首轴最外层（确定性）。"""

    axes: tuple[ParamAxis, ...]

    def __post_init__(self) -> None:
        if not self.axes:
            raise ValueError("ParamWindow 至少需要一条参数轴")
        seen: set[str] = set()
        for a in self.axes:
            if a.key in seen:
                raise ValueError(f"参数轴重复: {a.key}")
            seen.add(a.key)

    def axis(self, name: str) -> ParamAxis:
        for a in self.axes:
            if a.name == name:
                return a
        raise KeyError(f"窗口中不存在参数轴: {name}")

    def contains(self, point: "ParamPoint") -> bool:
        """点是否落在窗口内（逐轴判断；窗口没有的轴不约束）。"""
        for a in self.axes:
            v = point.values.get(a.key)
            if v is None or not (a.min_value <= v <= a.max_value):
                return False
        return True

    def grid_points(self) -> Iterator["ParamPoint"]:
        """按步进展开网格点。任一轴缺 step 时抛 ValueError（连续窗口用策略采样）。"""
        from itertools import product

        ranges = []
        for a in self.axes:
            if a.step_value is None or a.step_value <= 0:
                raise ValueError(f"轴 {a.name} 未定义有效 step，无法展开网格")
            n = int(round((a.max_value - a.min_value) / a.step_value))
            ranges.append([a.min_value + i * a.step_value for i in range(n + 1)])
        for combo in product(*ranges):
            yield ParamPoint(values={a.key: v for a, v in zip(self.axes, combo)})

    def to_contract_window(self) -> list[dict[str, Any]]:
        """导出为 experiment-scan 契约的 window 数组形状（FI 对齐）。"""
        rows = []
        for a in self.axes:
            rows.append({
                "axis": a.name,
                "min_value": a.min_value,
                "max_value": a.max_value,
                "step_value": a.step_value if a.step_value is not None else "unknown",
                "unit": a.unit,
            })
        return rows


@dataclass(frozen=True)
class ParamPoint:
    """一组具体注入参数。键名遵循 {axis}_{unit} 规范。"""

    values: dict[str, float]

    def get(self, axis: str, unit: str) -> float:
        return self.values[param_key(axis, unit)]

    def try_get(self, axis: str, unit: str) -> float | None:
        return self.values.get(param_key(axis, unit))


#: 通道基准权重（scan-acceleration.md §4.3；§2.5 允许 0-2x）。
BASE_CHANNEL_WEIGHTS: dict[str, float] = {
    "C1": 10.0,
    "C2": 12.0,
    "C3": 6.0,
    "C4": 8.0,
    "C5": 4.0,
}


@dataclass
class HyperParams:
    """策略超参（§2.5 白名单全集；越界即拒绝——range 校验后生效并落审计）。"""

    batch_size: int = 64                 # 16-256
    step_shrink: int = 8                 # 2-16
    theta_hot: float = 0.75              # P50-P95（分位数 0.50-0.95）
    epsilon: float = 0.10                # 0.05-0.20
    channel_weights: dict[str, float] = field(
        default_factory=lambda: dict(BASE_CHANNEL_WEIGHTS)
    )
    confirm_k: int = 3                   # 2-5

    def violations(self) -> list[str]:
        """返回全部越界描述（空列表 = 合法）。"""
        v: list[str] = []
        if not (16 <= self.batch_size <= 256):
            v.append(f"batch_size={self.batch_size} 超出白名单 16-256")
        if not (2 <= self.step_shrink <= 16):
            v.append(f"step_shrink={self.step_shrink} 超出白名单 2-16")
        if not (0.50 <= self.theta_hot <= 0.95):
            v.append(f"theta_hot={self.theta_hot} 超出白名单 P50-P95")
        if not (0.05 <= self.epsilon <= 0.20):
            v.append(f"epsilon={self.epsilon} 超出白名单 5-20%")
        if not (2 <= self.confirm_k <= 5):
            v.append(f"confirm_k={self.confirm_k} 超出白名单 2-5")
        for key, base in BASE_CHANNEL_WEIGHTS.items():
            w = self.channel_weights.get(key)
            if w is None:
                v.append(f"channel_weights 缺通道 {key}")
            elif not (0.0 <= w <= 2.0 * base):
                v.append(f"channel_weights[{key}]={w} 超出白名单 0-{2.0 * base}（基准 {base}）")
        unknown = set(self.channel_weights) - set(BASE_CHANNEL_WEIGHTS)
        if unknown:
            v.append(f"channel_weights 含未知通道: {sorted(unknown)}")
        return v

    def validate(self) -> None:
        """越界即拒绝（ValueError，含全部违例项）。"""
        v = self.violations()
        if v:
            raise ValueError("超参越界（白名单见 scan-acceleration.md §2.5）: " + "; ".join(v))

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any] | None = None, *, audit: Any = None, actor: str = "agent-hint"
    ) -> "HyperParams":
        """从 hint dict 应用超参（未知键机器拒绝；通过校验后落审计）。

        这是模型 Agent 改策略行为的**唯一**通道——射击坐标不接受自由文本。
        """
        params = cls()
        if data:
            unknown = set(data) - {"batch_size", "step_shrink", "theta_hot",
                                   "epsilon", "channel_weights", "confirm_k"}
            if unknown:
                raise ValueError(f"未知超参键（白名单外拒绝）: {sorted(unknown)}")
            if "batch_size" in data:
                params.batch_size = int(data["batch_size"])
            if "step_shrink" in data:
                params.step_shrink = int(data["step_shrink"])
            if "theta_hot" in data:
                params.theta_hot = float(data["theta_hot"])
            if "epsilon" in data:
                params.epsilon = float(data["epsilon"])
            if "confirm_k" in data:
                params.confirm_k = int(data["confirm_k"])
            if "channel_weights" in data:
                merged = dict(BASE_CHANNEL_WEIGHTS)
                merged.update({k: float(v) for k, v in dict(data["channel_weights"]).items()})
                params.channel_weights = merged
        params.validate()
        if audit is not None:
            audit.record(actor, "strategy.hint_applied", "hwctl.scan", params.to_dict())
        return params

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "step_shrink": self.step_shrink,
            "theta_hot": self.theta_hot,
            "epsilon": self.epsilon,
            "channel_weights": dict(self.channel_weights),
            "confirm_k": self.confirm_k,
        }


def window_from_grid(data: Mapping[str, Any]) -> tuple[ParamWindow, dict[str, Any]]:
    """解析 grid.json：{"axes": [{"name","unit","min","max","step"}], ...}。

    返回 (ParamWindow, meta)。meta 含 strategy / campaign_id（可缺省）。
    """
    axes_raw = data.get("axes")
    if not axes_raw or not isinstance(axes_raw, Sequence):
        raise ValueError("grid.json 缺 axes 数组")
    axes: list[ParamAxis] = []
    for a in axes_raw:
        axes.append(ParamAxis(
            name=str(a["name"]),
            unit=str(a.get("unit", "raw")),
            min_value=float(a["min"]),
            max_value=float(a["max"]),
            step_value=float(a["step"]) if a.get("step") is not None else None,
        ))
    meta = {
        "strategy": str(data.get("strategy", "uniform")),
        "campaign_id": str(data.get("campaign_id", "unknown")),
    }
    return ParamWindow(tuple(axes)), meta
