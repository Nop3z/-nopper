# -*- coding: utf-8 -*-
"""hwctl.scan：delay/width 搜索引擎三件套（scan-acceleration.md §6.2）。

- params.py：参数值对象（{axis}_{unit} 泛化键）+ 超参白名单校验（§2.5）
- strategy.py：策略器——只产参数序列 + 停止谓词（uniform 完整 / adaptive3 骨架）
- executor.py：执行器——单射状态机（复位分档 / 预 arm / 触发 / 判定 / 记账）
- judge.py：判定器——C1/C2/C3（+C5）通道采集 + outcome + heat_score

边界（§6.2）：策略器<->执行器只传"参数序列 + 停止谓词"；判定器只回"通道向量"；
任何组件不得越权写安全域配置。铁律：参数永远由策略器代码生成，模型 Agent 只能
通过 hint 改超参白名单（六项，范围校验后生效并落审计）。
"""

from hwctl.scan.params import (
    BASE_CHANNEL_WEIGHTS,
    HyperParams,
    ParamAxis,
    ParamPoint,
    ParamWindow,
    param_key,
    window_from_grid,
)
from hwctl.scan.strategy import (
    Adaptive3Strategy,
    ScanPlan,
    UniformStrategy,
)
from hwctl.scan.judge import (
    OUTCOMES,
    RESP_DIFF_FORMS,
    BaselineStats,
    MockShotJudge,
    ScriptedScenario,
    ShotJudge,
    Verdict,
    classify_diff,
    diff_code,
)
from hwctl.scan.executor import (
    TIMINGS_DRYRUN,
    TIMINGS_REAL,
    ShotExecutor,
    ShotRecord,
    Timings,
)

__all__ = [
    "BASE_CHANNEL_WEIGHTS",
    "HyperParams",
    "ParamAxis",
    "ParamPoint",
    "ParamWindow",
    "param_key",
    "window_from_grid",
    "Adaptive3Strategy",
    "ScanPlan",
    "UniformStrategy",
    "OUTCOMES",
    "RESP_DIFF_FORMS",
    "BaselineStats",
    "MockShotJudge",
    "ScriptedScenario",
    "ShotJudge",
    "Verdict",
    "classify_diff",
    "diff_code",
    "TIMINGS_DRYRUN",
    "TIMINGS_REAL",
    "ShotExecutor",
    "ShotRecord",
    "Timings",
]
