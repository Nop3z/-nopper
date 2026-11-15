# -*- coding: utf-8 -*-
"""UartGuarder 触发驱动（UART 字节串触发主链 + 反向 UART ping）。

- uart_cfg（波特率 2400-115200）/ engine_cfg（1-8B 匹配串）/ arm / state；
- 反向 UART ping：射击后目标存活检查（tool-integration.md §3 判定闭环），
  judge 的 C1（时延）/ C2（金标应答 diff）/ C5（boot 重试）数据源；
- C1 骨架阶段用 MockUartGuarderDevice：可注入假目标行为
  （normal 金标应答 / mutated 错字符应答 / silent_crash 超时+boot 重试）；
- 真机层 TODO(P0)：from_port() 延迟 import uart_guarder（COM 口）。
"""
from __future__ import annotations

from typing import Any, Callable

from hwctl.protocol import PingResult

#: 设备事实：波特率范围（tool-integration.md §1#3）
BAUD_MIN = 2400
BAUD_MAX = 115200

#: 假目标行为键（Mock 专用）
TARGET_NORMAL = "normal"
TARGET_MUTATED = "mutated"        # 错字符应答 -> judge 判 success（部分成功最便宜探测器）
TARGET_SUBSET = "subset"          # 应答缺 token -> resp_diff_form=subset
TARGET_SILENT_CRASH = "silent_crash"  # 超时 + boot 重试 -> judge 判 crash


class MockUartGuarderDevice:
    """uart_guarder 包行为等价 Mock + 假目标。

    设备侧等价：uart_cfg 波特率范围校验；engine_cfg 匹配串 1-8B；
    arm -> 'armed'；state 在模拟匹配后为 'triggered'。

    假目标（set_target）：judge ping 的数据源，可注入假响应/假崩溃——
    - normal：金标应答（RTT 可配）；
    - mutated：错字符应答（C2 wrong_chars -> success 链路）；
    - subset：缺 token 应答（C2 subset -> success 链路）；
    - silent_crash：无应答 + boot_retry_count 递增（C1/C5 -> crash 链路）；
    - callable：fn(payload) -> PingResult（完全自定义）。
    """

    def __init__(self, *, golden_response: bytes = b"OK", rtt_ms: float = 5.0) -> None:
        self.golden_response = bytes(golden_response)
        self.rtt_ms = float(rtt_ms)
        self.baud: int | None = None
        self.matches: dict[int, bytes] = {}
        self._armed = False
        self.match_count = 0
        self.target: Any = TARGET_NORMAL
        self.boot_retry_count = 0
        self.calls: list[tuple[str, tuple, dict]] = []

    # ---- 设备原语（与 uart_guarder 包同签名风格）----

    def uart_cfg(self, baud: int) -> None:
        if not (BAUD_MIN <= baud <= BAUD_MAX):
            raise ValueError(f"baud 超出设备范围 {BAUD_MIN}-{BAUD_MAX}: {baud}")
        self.calls.append(("uart_cfg", (baud,), {}))
        self.baud = int(baud)

    def engine_cfg(self, engine: Any, match_bytes: bytes) -> None:
        mb = bytes(match_bytes)
        if not (1 <= len(mb) <= 8):
            raise ValueError("match bytes 须为 1-8 字节")
        self.calls.append(("engine_cfg", (engine, mb), {}))
        self.matches[int(engine) if isinstance(engine, int) else hash(str(engine))] = mb

    def arm(self) -> None:
        self.calls.append(("arm", (), {}))
        self._armed = True

    def state(self) -> str:
        if self._armed:
            return "armed"
        return "idle"

    # ---- 假目标（Mock 专用；judge ping 数据源）----

    def set_target(self, behavior: Any) -> None:
        """注入假目标行为：normal / mutated / subset / silent_crash / callable。"""
        self.target = behavior

    def ping(self, payload: bytes = b"PING", timeout_ms: int = 200) -> PingResult:
        """反向 UART ping：走假目标行为，产出 judge 数据源。"""
        self.calls.append(("ping", (bytes(payload),), {"timeout_ms": timeout_ms}))
        target = self.target
        if callable(target):
            return target(bytes(payload))
        if target == TARGET_SILENT_CRASH:
            self.boot_retry_count += 1
            self._armed = False
            return PingResult(responded=False, latency_ms=None, response_bytes=None,
                              boot_retry_count=self.boot_retry_count)
        self.match_count += 1
        if target == TARGET_MUTATED:
            resp = bytes(reversed(self.golden_response)) or b"KO"
            return PingResult(responded=True, latency_ms=self.rtt_ms, response_bytes=resp,
                              boot_retry_count=0)
        if target == TARGET_SUBSET:
            resp = self.golden_response[: max(1, len(self.golden_response) // 2)]
            return PingResult(responded=True, latency_ms=self.rtt_ms, response_bytes=resp,
                              boot_retry_count=0)
        # normal：金标应答
        return PingResult(responded=True, latency_ms=self.rtt_ms,
                          response_bytes=self.golden_response, boot_retry_count=0)


class UartGuarderTrigger:
    """TriggerDriver 实现（六件套：configure / arm / state；ping 为判定数据源）。"""

    def __init__(
        self,
        device: Any,
        *,
        engine: int = 1,
        audit: Any = None,
        actor: str = "hwctl.trigger.uart_guarder",
    ) -> None:
        self.device = device
        self.engine = int(engine)
        self.audit = audit
        self.actor = actor

    def configure(self, *, match_bytes: bytes, baud: int = 115200, engine: int | None = None,
                  data_bits: int = 8, parity: str = "none", stop_bits: int = 1) -> None:
        """字节串触发配置（六件套 1）。设备为 8N1 固定（tool-integration.md §1#3）。"""
        eng = self.engine if engine is None else int(engine)
        self.device.uart_cfg(int(baud))
        self.device.engine_cfg(eng, bytes(match_bytes))
        self._audit("trigger.configure", f"uart_guarder[engine={eng}]", {
            "match_bytes": bytes(match_bytes).hex(), "baud": int(baud),
            "data_bits": data_bits, "parity": parity, "stop_bits": stop_bits,
        })

    def arm(self) -> None:
        """布防（六件套 2）：TRI 输出使能，等待匹配。"""
        self.device.arm()
        self._audit("trigger.arm", "uart_guarder", {"engine": self.engine})

    def state(self) -> str:
        """设备状态（六件套 3）。"""
        return str(self.device.state())

    def ping(self, payload: bytes = b"PING", timeout_ms: int = 200) -> PingResult:
        """反向 UART ping（judge 目标存活检查数据源）。"""
        return self.device.ping(payload, timeout_ms)

    def _audit(self, action: str, device: str, detail: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.record(self.actor, action, device, detail)

    @classmethod
    def from_port(cls, port: str, **kwargs: Any) -> "UartGuarderTrigger":
        """真机接入 TODO(P0)：延迟 import uart_guarder（无设备环境不触发）。"""
        try:
            import uart_guarder as ug  # noqa: 延迟 import
        except ImportError as exc:
            raise RuntimeError(
                "uart_guarder 包未安装——真机层 TODO(P0)。"
                "C1 骨架请用 device=MockUartGuarderDevice()。"
            ) from exc
        device = ug.UartGuarder(port)  # 真机参数待人工校验
        return cls(device, **kwargs)
