# -*- coding: utf-8 -*-
"""hwctl.drivers：四类设备驱动（RigDriver 规范接口的各实现 + 行为等价 Mock）。

- powershorter.py：注入 rig（voltage mode 优先；包装 FI PowerShorterInjector 语义）
- uart_guarder.py：UART 字节串触发 + 反向 ping
- pico3000.py：scope 采集六件套
- shub.py：6 路供电开关 + V/I 测量（pwr 层首实现）

C1 骨架阶段全部用 Mock 跑链路；真机层 from_port()/from_serial() 延迟 import
对应 OSR 包（power_shorter / uart_guarder / pico3000 / shub_ctrl），
无设备环境不触发 import，CI 可测试。
"""

from hwctl.drivers.powershorter import (
    MockPowerShorterDevice,
    PowerShorterRig,
    DEFAULT_MAX_INJECTIONS,
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    DEFAULT_MAX_PULSE_RAW,
)
from hwctl.drivers.uart_guarder import (
    BAUD_MAX,
    BAUD_MIN,
    MockUartGuarderDevice,
    UartGuarderTrigger,
)
from hwctl.drivers.pico3000 import MockPico3000Scope, Pico3000Scope
from hwctl.drivers.shub import MockSmartHub, SmartHubPwr

__all__ = [
    "MockPowerShorterDevice",
    "PowerShorterRig",
    "DEFAULT_MAX_INJECTIONS",
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "DEFAULT_MAX_PULSE_RAW",
    "BAUD_MIN",
    "BAUD_MAX",
    "MockUartGuarderDevice",
    "UartGuarderTrigger",
    "MockPico3000Scope",
    "Pico3000Scope",
    "MockSmartHub",
    "SmartHubPwr",
]
