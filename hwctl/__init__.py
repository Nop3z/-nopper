# -*- coding: utf-8 -*-
"""hwctl：nopper 硬件控制桥（fork-plan §8 三层桥的桥接层）。

分层职责：
- shell 命令层（零改造，不在本包）；
- **hwctl 桥接层（本包）**：rig / trigger / pwr / scope 四类驱动 + 安全域互锁
  + CSV 台账 + 审计日志——Agent 只见 hwctl 受控接口；
- 标准栈层：pyvisa / pyserial / hidapi 藏在 drivers 后面，换设备型号不改 prompt。

C1 骨架阶段约定：
- 全部驱动以 Mock 行为等价实现（可注入假响应/假崩溃），真机层留 TODO 接口；
- 串口/HID 依赖一律延迟 import，无设备环境（Windows Python 3.12 / CI）可测试；
- 安全域配置文件（默认 ./safety-domain.json）对 Agent 只读（条令约定），
  本包不提供任何写回接口；
- 铁律：结果 fact 落图前，台账必须已 flush + fsync（ShotLedger.flush_before）。
"""

__version__ = "0.1.0"

from hwctl.protocol import (
    DURATION_UNIT_NS,
    UNIT_SOURCE,
    SHOT_LIFECYCLE,
    InjectionReceipt,
    PingResult,
    ShotRequest,
    RigDriver,
    PwrDriver,
    ScopeDriver,
    TriggerDriver,
    build_glitch_pattern,
    request_from_point,
    utc_now_iso,
)
from hwctl.safety import (
    SafetyDomain,
    SafetyViolation,
    XorInvariant,
    validate_xor_mask,
)
from hwctl.ledger import (
    LEDGER_COLUMNS,
    AuditLog,
    ShotLedger,
)

__all__ = [
    "__version__",
    "DURATION_UNIT_NS",
    "UNIT_SOURCE",
    "SHOT_LIFECYCLE",
    "InjectionReceipt",
    "PingResult",
    "ShotRequest",
    "RigDriver",
    "PwrDriver",
    "ScopeDriver",
    "TriggerDriver",
    "build_glitch_pattern",
    "request_from_point",
    "utc_now_iso",
    "SafetyDomain",
    "SafetyViolation",
    "XorInvariant",
    "validate_xor_mask",
    "LEDGER_COLUMNS",
    "AuditLog",
    "ShotLedger",
]
