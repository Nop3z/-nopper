# -*- coding: utf-8 -*-
"""扫描策略器（只产参数序列 + 停止谓词；脚本生成、无 LLM；scan-acceleration.md §2/§6.2）。

- UniformStrategy：均匀网格全展开（MVP 完整实现；全量程全覆盖保底）。
- Adaptive3Strategy：三阶段几何法骨架（社区事实标准；v1 全量，本期接口+文档）：
  Stage1 粗扫（非自适应全覆盖，保底覆盖比 beta）->
  Stage2 热区细扫（theta_hot=P75 相对阈值 + 邻接 success 强制 + crash 过渡带强制，
  步进 /step_shrink 逐轮减半至硬件分辨率）->
  Stage3 边界二分（k=confirm_k 确认发、<=8 轮收敛，输出 window_min/max + 每边成功率）。

铁律：参数永远由策略器代码生成；模型 Agent 只通过 hint 改超参白名单
（§2.5 六项，HyperParams 范围校验后生效并落审计）。BO（skopt GP-EI）为 v2
可选对照，接口同本模块（bayes.py，中位射击量不降 >=2x 就删）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from hwctl.scan.params import HyperParams, ParamPoint, ParamWindow


@dataclass
class ScanPlan:
    """策略器 -> 执行器的唯一交付物：参数序列 + 停止谓词（§6.2 边界）。"""

    strategy: str
    points: list[ParamPoint]
    batch_size: int
    stop: Callable[[int, Sequence[str]], bool]
    """stop(shots_done, outcomes) -> bool：True = 提前停（预算/收敛判定）。"""
    meta: dict[str, Any] = field(default_factory=dict)


class UniformStrategy:
    """均匀网格（MVP 完整实现）。

    全量程等步进全覆盖（ParamWindow.grid_points 语义：首轴最外层，确定性顺序），
    点数 = 各轴 (max-min)/step+1 之积；批次按 hyper.batch_size 切；
    shot_budget 裁剪与安全域对齐（safety.shot_budget 为最终上限）。
    """

    name = "uniform"

    def __init__(self, *, hyper: HyperParams | None = None) -> None:
        self.hyper = hyper if hyper is not None else HyperParams()
        self.hyper.validate()

    def plan(self, window: ParamWindow, *, shot_budget: int | None = None) -> ScanPlan:
        points = list(window.grid_points())
        if not points:
            raise ValueError("网格展开为空（检查 axes min/max/step）")
        if shot_budget is not None and shot_budget < len(points):
            points = points[: int(shot_budget)]
        n = len(points)
        batch = max(1, min(int(self.hyper.batch_size), n))

        def stop(shots_done: int, outcomes: Sequence[str]) -> bool:
            return shots_done >= n

        return ScanPlan(
            strategy=self.name,
            points=points,
            batch_size=batch,
            stop=stop,
            meta={"grid_axes": [a.key for a in window.axes], "planned_shots": n},
        )


class Adaptive3Strategy:
    """三阶段几何法骨架（v1 全量落地；本期接口 + 文档）。

    Stage1 coarse  ：均匀粗扫全量程（保底覆盖比 beta——先验失误时的回退保障；
                      MVP 直接全量均匀，beta 截断 v1 落地）。
    Stage2 refine  ：热区收缩细扫。热区判定（§2.3）：theta_hot=P75 相对阈值
                      + 邻接 success 强制 + crash 过渡带强制；步进 /step_shrink
                      逐轮减半至硬件分辨率（delay<=2us、width<=20ns）。
                      批内预算分配（§4.4）：top-K 未打热区格（贪心）
                      + ceil(epsilon*B) 随机未覆盖格（epsilon-greedy 抗局部陷阱）；
                      纯 crash 平台连续命中 >=20% 批预算时本批不再追加强度轴。
    Stage3 confirm ：边界二分（k=confirm_k 确认发、<=8 轮收敛），输出
                      window_min/max + 每边成功率，交接 dispatch-verify-repro
                      （窗口包含 rig 上下文：v_target/探针/通道权重）。
    """

    name = "adaptive3"

    def __init__(self, *, hyper: HyperParams | None = None) -> None:
        self.hyper = hyper if hyper is not None else HyperParams()
        self.hyper.validate()

    def plan(self, window: ParamWindow, *, shot_budget: int | None = None) -> ScanPlan:
        """骨架路径：先跑 Stage1 粗扫；Stage2/3 见 plan_stage2_refine / plan_stage3_confirm。"""
        plan1 = self.plan_stage1_coarse(window, shot_budget=shot_budget)
        plan1.meta["adaptive3"] = (
            "stage1-only（C1 骨架）：stage2/3 接口已留，见 plan_stage2_refine/plan_stage3_confirm"
        )
        return plan1

    def plan_stage1_coarse(
        self, window: ParamWindow, *, shot_budget: int | None = None
    ) -> ScanPlan:
        """Stage1 粗扫：非自适应全覆盖（复用 uniform；v1 加 beta 保底覆盖截断）。"""
        return UniformStrategy(hyper=self.hyper).plan(window, shot_budget=shot_budget)

    def plan_stage2_refine(
        self,
        window: ParamWindow,
        coarse_verdicts: Sequence[Mapping[str, Any]],
        *,
        step_shrink: int | None = None,
    ) -> ScanPlan:
        """Stage2 热区收缩细扫（v1 TODO）。

        输入：Stage1 判定向量（含通道 z 值与 heat_score）。
        产出：细扫参数序列（步进 /step_shrink 减半逐轮，theta_hot 热区 +
        过渡带 + epsilon-greedy 随机覆盖）。
        """
        raise NotImplementedError(
            "adaptive3 Stage2（热区收缩）为 v1 落点（scan-acceleration.md §2.3/§4.4）；"
            "C1 骨架用 uniform。"
        )

    def plan_stage3_confirm(
        self,
        window: ParamWindow,
        edge_candidates: Sequence[Mapping[str, Any]],
        *,
        confirm_k: int | None = None,
    ) -> ScanPlan:
        """Stage3 边界二分 + 确认发（v1 TODO）。

        输入：Stage2 产出的窗口边缘候选（每边）。
        产出：二分确认射击序列（k 发确认、<=8 轮收敛）+
        交接 dispatch-verify-repro 的窗口包（window_min/max + 每边成功率）。
        """
        raise NotImplementedError(
            "adaptive3 Stage3（边界二分）为 v1 落点（scan-acceleration.md §2.1）；"
            "C1 骨架用 uniform。"
        )
