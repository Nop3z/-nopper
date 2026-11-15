# -*- coding: utf-8 -*-
"""CSV 台账（异步写、批尾 flush+fsync）+ 审计日志。

铁律（scan-acceleration.md §3.2 #8 / fork-plan §12.3）：记录先于相信——
**结果 fact 落图前，台账必须已 flush + fsync**。接口：

- ShotLedger.append(row)：异步入队，返回单调递增 shot_id；
- ShotLedger.flush_before(shot_id)：阻塞直到该行及其之前全部行落盘（fact 落图前调用）；
- ShotLedger.flush_all()：批尾 flush + fsync。

列名对齐 FI campaign-record（delay_raw/pulse_raw/delay_ns/pulse_ns、
state_label/responded 原样透传，语义 unconfirmed-v0.1）+ C1-C5 通道列
（scan-acceleration.md §4.1）+ shot_id/timestamp（任务要求）。

审计日志为 JSONL，逐条写透（flush+fsync），可完整重放一次实验会话
（fork-plan E4 互锁与审计验收）。
"""
from __future__ import annotations

import csv
import json
import os
import queue
import threading
from typing import Any, Mapping, Sequence

from hwctl.protocol import utc_now_iso

#: 台账列（对齐 FI campaign-record 字段 + C1-C5 通道 + 台账元数据）。
LEDGER_COLUMNS: tuple[str, ...] = (
    "shot_id", "timestamp", "campaign_id", "shot_index", "strategy", "batch_id",
    # FI campaign-record params 对齐（raw 刻度必填；ns 为显式换算）
    "delay_raw", "pulse_raw", "delay_ns", "pulse_ns",
    # FI campaign-record response 对齐（state_label 原样透传，语义 unconfirmed-v0.1）
    "outcome", "state_label", "responded",
    # 单位纪律（显式换算，不隐式假设）
    "unit_scale", "unit_source", "pattern_json",
    # C1 UART ping 时延 / C2 应答 diff 形态（exact/subset/extra_tokens/wrong_chars/silent，编码 0-4）
    "resp_latency_ms", "resp_diff_form", "resp_diff_code",
    # C3 电流谷值深度与谷值时刻（相对触发事件）
    "valley_ma", "valley_offset_ms",
    # C4 轨塌陷特征（scope 抽样射才采，缺省空）
    "sag_depth_mv", "sag_duration_us", "rail_recovery_ms",
    # C5 boot 重试次数
    "boot_retry_count",
    # 热区分（§4.3 每射行；crash 过渡带/平台为批级项，v1 补）
    "heat_score",
    # 复位分档 / 安全拒绝原因
    "reset_mode", "error",
)


def _normalize_cell(value: Any) -> Any:
    """CSV 单元格归一：bool -> true/false；None -> 空串（通道不可用/未采）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


class ShotLedger:
    """异步 CSV 台账（单写线程；批内攒写、批尾 flush+fsync）。

    flush_before(shot_id) 语义：返回时，shot_id <= 给定值的行**保证已在磁盘上**
    （write + flush + os.fsync 完成）——结果 fact 落图前必须先调它。
    """

    def __init__(
        self,
        path: str,
        *,
        columns: Sequence[str] = LEDGER_COLUMNS,
        audit: Any = None,
    ) -> None:
        self.path = path
        self.columns = tuple(columns)
        self.audit = audit
        self._lock = threading.Lock()
        self._closed = False
        self._queue: queue.Queue = queue.Queue()
        self._next_id = 1
        self._durable_id = 0     # 已 fsync 到磁盘的最大 shot_id
        self._written = 0
        self._error: BaseException | None = None

        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=list(self.columns), extrasaction="ignore")
        if new_file:
            self._writer.writeheader()
            self._fh.flush()
            os.fsync(self._fh.fileno())
        self._thread = threading.Thread(target=self._run, name="hwctl-shot-ledger", daemon=True)
        self._thread.start()
        if audit is not None:
            audit.record("hwctl.ledger", "ledger.open", path, {"columns": len(self.columns)})

    # ---------- 提交侧（调用方线程） ----------

    def append(self, row: Mapping[str, Any]) -> int:
        """异步追加一行，返回单调递增的 shot_id。不等待落盘。"""
        if self._closed:
            raise RuntimeError("ledger 已关闭，拒绝追加")
        if self._error is not None:
            raise RuntimeError(f"ledger 写线程已失败: {self._error}") from self._error
        with self._lock:
            shot_id = self._next_id
            self._next_id += 1
            normalized = {k: _normalize_cell(row.get(k, "")) for k in self.columns}
            normalized["shot_id"] = shot_id
            self._queue.put(("row", normalized))
            return shot_id

    def flush_before(self, shot_id: int) -> None:
        """阻塞直到 shot_id <= 给定值的行全部写盘并 fsync（**fact 落图铁律接口**）。"""
        if self._error is not None:
            raise RuntimeError(f"ledger 写线程已失败: {self._error}") from self._error
        ev = threading.Event()
        with self._lock:
            if self._durable_id >= int(shot_id):
                return  # 已经落盘，直接返回
            self._queue.put(("flushreq", int(shot_id), ev))
        ev.wait()
        if self._error is not None:
            raise RuntimeError(f"ledger 写线程已失败: {self._error}") from self._error

    def flush_all(self) -> None:
        """批尾 flush + fsync（等价 flush_before(最后分配的 shot_id)）。"""
        with self._lock:
            last = self._next_id - 1
        if last > 0:
            self.flush_before(last)

    def durable_shot_id(self) -> int:
        """当前已 fsync 到磁盘的最大 shot_id。"""
        with self._lock:
            return self._durable_id

    def rows_written(self) -> int:
        """已写盘的数据行数（不含表头）。"""
        with self._lock:
            return self._written

    def close(self) -> None:
        """批尾 flush + fsync 后停写线程并关文件（幂等）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.flush_all()
        self._queue.put(None)
        self._thread.join(timeout=10)
        self._fh.close()
        if self.audit is not None:
            self.audit.record(
                "hwctl.ledger", "ledger.close", self.path,
                {"rows_written": self._written, "durable_shot_id": self._durable_id},
            )

    def __enter__(self) -> "ShotLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- 写线程 ----------

    def _run(self) -> None:
        pending_flush: list[tuple[int, threading.Event]] = []
        try:
            while True:
                item = self._queue.get()
                batch = [] if item is None else [item]
                if item is not None:
                    while True:
                        try:
                            batch.append(self._queue.get_nowait())
                        except queue.Empty:
                            break
                rows: list[dict[str, Any]] = []
                for entry in batch:
                    if entry[0] == "row":
                        rows.append(entry[1])
                    elif entry[0] == "flushreq":
                        pending_flush.append((entry[1], entry[2]))
                if rows:
                    for r in rows:
                        self._writer.writerow(r)
                    self._fh.flush()
                    os.fsync(self._fh.fileno())  # 批尾 fsync：记录先于相信
                    with self._lock:
                        self._written += len(rows)
                        self._durable_id = max(self._durable_id, int(rows[-1]["shot_id"]))
                # 满足已达到落盘要求的 flush 请求，未达到的继续等
                still: list[tuple[int, threading.Event]] = []
                with self._lock:
                    durable = self._durable_id
                for sid, ev in pending_flush:
                    if durable >= sid:
                        ev.set()
                    else:
                        still.append((sid, ev))
                pending_flush = still
                if item is None:
                    break
        except BaseException as exc:  # noqa: BLE001 —— 写线程异常必须浮出给调用方
            self._error = exc
            for _sid, ev in pending_flush:
                ev.set()


class AuditLog:
    """审计日志（JSONL，逐条 flush+fsync）：谁在何时对什么设备做了什么。

    fork-plan E4 验收：审计日志可完整重放一次实验会话——逐条写透，不攒批。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", encoding="utf-8")
        if new_file:
            self._record_unlocked(
                "hwctl.ledger", "audit.open", path, {}, notify=False
            )

    def _record_unlocked(
        self, actor: str, action: str, device: str, detail: Mapping[str, Any] | None,
        *, notify: bool = True,
    ) -> dict[str, Any]:
        entry = {
            "ts": utc_now_iso(),
            "actor": actor,
            "action": action,
            "device": device,
            "detail": dict(detail or {}),
        }
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return entry

    def record(
        self, actor: str, action: str, device: str, detail: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """记录一条审计事件（同步写透：flush + fsync 后返回）。"""
        with self._lock:
            return self._record_unlocked(actor, action, device, detail)

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
