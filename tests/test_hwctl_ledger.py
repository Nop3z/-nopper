# -*- coding: utf-8 -*-
"""台账测试：异步写、批尾 flush+fsync、flush_before(fact_id) 铁律顺序、审计日志。"""

import csv
import json

import pytest

from hwctl.ledger import LEDGER_COLUMNS, AuditLog, ShotLedger


def read_rows(path):
    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    return rows[0], rows[1:]


def make_row(i):
    return {
        "timestamp": "2026-11-15T08:00:00.000+00:00",
        "campaign_id": "t",
        "shot_index": i,
        "delay_raw": 100 + i,
        "pulse_raw": 2,
        "delay_ns": (100 + i) * 10,
        "pulse_ns": 20,
        "outcome": "no_effect",
        "responded": True,
        "unit_scale": 10,
        "unit_source": "power_shorter.engine_cfg docstring: duration is in unit of 10ns",
        "pattern_json": json.dumps([[1, 100 + i], [0, 2], [1, 1]], separators=(",", ":")),
        "boot_retry_count": None,      # 通道未采 -> 空串
        "heat_score": 0.0,
    }


def test_append_async_then_flush_before_durability(tmp_path):
    path = tmp_path / "ledger.csv"
    with ShotLedger(str(path)) as led:
        ids = [led.append(make_row(i)) for i in range(100)]
        assert ids == list(range(1, 101))  # shot_id 单调递增从 1
        # flush 前磁盘行数不保证（异步）——铁律接口返回后必须全部落盘
        led.flush_before(100)
        header, rows = read_rows(path)
        assert header == list(LEDGER_COLUMNS)
        assert len(rows) == 100
        assert [int(r[0]) for r in rows] == list(range(1, 101))
        assert led.durable_shot_id() == 100
        assert led.rows_written() == 100


def test_flush_before_guarantees_up_to_given_id(tmp_path):
    path = tmp_path / "ledger.csv"
    with ShotLedger(str(path)) as led:
        led.append(make_row(0))
        led.flush_before(1)
        _header, rows = read_rows(path)
        assert len(rows) >= 1
        # 再追加一批，flush 到第 6 行即可保证前 6 行 durable（允许更多已落盘）
        for i in range(1, 10):
            led.append(make_row(i))
        led.flush_before(6)
        _header, rows = read_rows(path)
        ids_on_disk = [int(r[0]) for r in rows]
        assert ids_on_disk[:6] == [1, 2, 3, 4, 5, 6]
        assert led.durable_shot_id() >= 6


def test_flush_all_is_batch_tail_fsync(tmp_path):
    path = tmp_path / "ledger.csv"
    with ShotLedger(str(path)) as led:
        for i in range(20):
            led.append(make_row(i))
        led.flush_all()
        _header, rows = read_rows(path)
        assert len(rows) == 20


def test_row_normalization(tmp_path):
    path = tmp_path / "ledger.csv"
    with ShotLedger(str(path)) as led:
        shot_id = led.append(make_row(0))
        led.flush_before(shot_id)
    header, rows = read_rows(path)
    col = {name: idx for idx, name in enumerate(header)}
    row = dict(zip(header, rows[0]))
    assert row["responded"] == "true"          # bool -> true/false
    assert row["boot_retry_count"] == ""       # None -> 空串（通道未采）
    assert row["unit_scale"] == "10"
    pattern = json.loads(row["pattern_json"])
    assert pattern == [[1, 100], [0, 2], [1, 1]]  # pattern 原样（JSON round-trip）


def test_unknown_extra_keys_ignored(tmp_path):
    path = tmp_path / "ledger.csv"
    with ShotLedger(str(path)) as led:
        row = make_row(0)
        row["some_future_column"] = "x"  # 额外键不进 CSV（列固定）
        shot_id = led.append(row)
        led.flush_before(shot_id)
    header, rows = read_rows(path)
    assert "some_future_column" not in header
    assert len(rows) == 1


def test_append_after_close_rejected(tmp_path):
    path = tmp_path / "ledger.csv"
    led = ShotLedger(str(path))
    led.close()
    with pytest.raises(RuntimeError):
        led.append(make_row(0))


def test_context_manager_flushes_on_exit(tmp_path):
    path = tmp_path / "ledger.csv"
    with ShotLedger(str(path)) as led:
        led.append(make_row(0))
        # 不显式 flush——close（__exit__）必须批尾 flush+fsync
    _header, rows = read_rows(path)
    assert len(rows) == 1


def test_audit_log_jsonl_write_through(tmp_path):
    path = tmp_path / "audit.jsonl"
    with AuditLog(str(path)) as audit:
        audit.record("hwctl.test", "action.a", "dev1", {"k": 1})
        audit.record("hwctl.test", "action.b", "dev2")
        # 逐条写透：无需 flush 接口，record 返回即已 fsync
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert [e["action"] for e in entries] == ["audit.open", "action.a", "action.b"]
    for e in entries:
        assert set(e) == {"ts", "actor", "action", "device", "detail"}
        assert e["ts"].startswith("20") and ("+" in e["ts"])  # ISO 带时区
    assert entries[1]["detail"] == {"k": 1}
    assert entries[2]["detail"] == {}
