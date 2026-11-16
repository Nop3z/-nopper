# -*- coding: utf-8 -*-
"""hwctl 命令面（Agent 受控入口；dry-run 模式全链可跑）。

用法（Windows Python 3.12，仓库根目录）：
    python -m hwctl scan --grid grid.json --dry-run
    python -m hwctl devices

- scan：加载 grid.json -> 策略展开 -> Mock 全链（rig/trigger/pwr/scope + judge
  场景）-> CSV 台账 + 审计日志。--dry-run 全程无硬件；无 --dry-run 时
  C1 骨架直接拒绝（真机层 TODO P0/P1）。
- devices：逐包延迟 import 探测真机包（power_shorter / uart_guarder /
  pico3000 / shub_ctrl）；无设备环境输出空 JSON 数组。

安全域：--safety 指定现场文件（默认 ./safety-domain.json）；不存在时回退
随包只读缺省档并落审计。该文件对 Agent 只读（条令约定）。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
from typing import Any

from hwctl.drivers import (
    MockPico3000Scope,
    MockPowerShorterDevice,
    MockSmartHub,
    MockUartGuarderDevice,
    Pico3000Scope,
    PowerShorterRig,
    SmartHubPwr,
    UartGuarderTrigger,
)
from hwctl.ledger import AuditLog, ShotLedger
from hwctl.safety import SafetyDomain, SafetyViolation
from hwctl.scan.executor import TIMINGS_DRYRUN, ShotExecutor
from hwctl.scan.judge import MockShotJudge, ScriptedScenario
from hwctl.scan.params import HyperParams, window_from_grid
from hwctl.scan.strategy import Adaptive3Strategy, UniformStrategy

#: 真机包探测表（模块名 -> hwctl 分层；延迟 import，缺包即跳过）
DEVICE_PROBES: tuple[tuple[str, str, str], ...] = (
    ("power_shorter", "rig", "PowerShorter"),
    ("uart_guarder", "trigger", "UartGuarder"),
    ("pico3000", "scope", "PicoScope3000"),
    ("shub_ctrl", "pwr", "OSR_Smart_HUB"),
)

DEFAULT_MATCH_BYTES = "AABBCC"  # tool-integration.md §3.1 明文标识示例


def _usb_vid_present(vid: str) -> bool:
    """Windows: 检查指定 VID 的 USB 设备是否已连接（pnputil）。"""
    try:
        out = subprocess.run(["pnputil", "/enum-devices", "/connected"], capture_output=True, text=True, timeout=60, shell=True)
        return ("VID_" + vid.upper()) in (out.stdout + out.stderr).upper()
    except Exception:  # noqa: BLE001
        return False

def probe_devices(port: str | None = None) -> list[dict[str, Any]]:
    """设备探测：逐包延迟 import；--port 提供时对 power_shorter 做真实打开+state 读取。"""
    found: list[dict[str, Any]] = []
    for module_name, layer, label in DEVICE_PROBES:
        try:
            importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 —— import 侧失败一律视为无此设备
            continue
        entry: dict[str, Any] = {
            "kind": layer,
            "module": module_name,
            "label": label,
            "status": "module-available",
        }
        # 真实探测：仅 power_shorter 且显式提供 COM 口时（只读 state，不 arm/不触发）
        if port and module_name == "power_shorter":
            try:
                dev = importlib.import_module("power_shorter").PowerShorter(port)
                st = {e.name: dev.state(e) for e in importlib.import_module("power_shorter").Engine}
                entry["status"] = "connected"
                entry["port"] = port
                entry["engines"] = st
                entry["detail"] = "real-device state probe (read-only)"
            except Exception as exc:  # noqa: BLE001 —— 打不开如实报告
                entry["status"] = "open-failed"
                entry["port"] = port
                entry["detail"] = f"{type(exc).__name__}: {exc}"
        else:
            entry["detail"] = "真机设备枚举 TODO(P0/P1)：需打开 COM/HID 逐台确认"
        found.append(entry)
    # Glasgow: USB 设备级探测（VID 1d50 Glasgow Interface Explorer；不需要 python 包）
    try:
        if _usb_vid_present("1D50") or _usb_vid_present("20B7"):
            found.append({
                "kind": "rig",
                "module": "glasgow",
                "label": "Glasgow Interface Explorer",
                "status": "usb-connected",
                "detail": "USB 可见（OSR-Glasgow 固件 VID_20B7 或原版 1D50；python 包 glasgow 未装，CLI 层 TODO）",
            })
    except Exception:  # noqa: BLE001
        pass
    return found

def cmd_devices(args: argparse.Namespace) -> int:
    print(json.dumps(probe_devices(getattr(args, "port", None)), ensure_ascii=False, indent=2))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print(
            "[hwctl] 真机模式未接入（C1 骨架：真机层 TODO P0/P1）。"
            "请加 --dry-run 走 Mock 全链。",
            file=sys.stderr,
        )
        return 2

    with open(args.grid, "r", encoding="utf-8") as fh:
        grid_data = json.load(fh)
    window, grid_meta = window_from_grid(grid_data)

    with AuditLog(args.audit) as audit:
        audit.record("hwctl.cli", "scan.start", args.grid, {
            "grid_axes": [a.key for a in window.axes],
            "strategy": args.strategy or grid_meta["strategy"],
            "campaign_id": args.campaign or grid_meta["campaign_id"],
            "dry_run": True,
        })

        # 超参：CLI hint 走白名单校验（越界 ValueError 即拒绝）+ 落审计
        hint: dict[str, Any] = {}
        if args.batch_size is not None:
            hint["batch_size"] = args.batch_size
        hyper = HyperParams.from_dict(hint, audit=audit, actor="cli-hint")

        # 安全域：现场文件优先，缺省回退随包只读档（回退事实落审计）
        safety = SafetyDomain.load_default(args.safety, audit=audit)

        # ---- Mock 全链组装（C1 骨架；真机层 TODO P0/P1）----
        mock_ps = MockPowerShorterDevice()
        rig = PowerShorterRig(mock_ps, audit=audit)
        mock_ug = MockUartGuarderDevice(golden_response=b"OK", rtt_ms=5.0)
        trigger = UartGuarderTrigger(mock_ug, audit=audit)
        trigger.configure(match_bytes=bytes.fromhex(args.match_bytes), baud=115200)
        mock_shub = MockSmartHub()
        pwr = SmartHubPwr(mock_shub, audit=audit)
        pwr.set_power(1, True)  # 目标供电通道上电（物理动作，落审计；Mock 无电）
        mock_scope_dev = MockPico3000Scope()
        scope = Pico3000Scope(mock_scope_dev, audit=audit)
        scope.configure(("A",), sample_rate_hz=125e6, trigger_source="EXT")

        strategy_name = args.strategy or grid_meta["strategy"]
        if strategy_name == "adaptive3":
            strategy = Adaptive3Strategy(hyper=hyper)
        else:
            strategy = UniformStrategy(hyper=hyper)

        ledger = ShotLedger(args.out, audit=audit)
        try:
            scenario = ScriptedScenario(window, uart=mock_ug, pwr=mock_shub)
            judge = MockShotJudge(uart=mock_ug, pwr=pwr, prepare=scenario.prepare)
            executor = ShotExecutor(
                rig, trigger, judge, ledger, safety, audit,
                pwr=pwr,
                scope=scope,
                timings=TIMINGS_DRYRUN,
                campaign_id=args.campaign or grid_meta["campaign_id"],
                strategy_name=strategy.name,
                scope_sample_every=args.scope_sample_every,
                scope_dir=os.path.dirname(os.path.abspath(args.out)),
            )
            plan = strategy.plan(window, shot_budget=safety.shot_budget)
            records = executor.run(plan)

            # 铁律演示：结果 fact 落图前台账必须已 flush（此处确保最后一射 durable）
            last_id = records[-1].shot_id if records else None
            if last_id is not None:
                executor.ensure_flushed(last_id)

            counts: dict[str, int] = {}
            for r in records:
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
            summary = {
                "strategy": strategy.name,
                "planned": len(plan.points),
                "executed": len(records),
                "outcomes": counts,
                "shot_budget_used": safety.shots_used,
                "interlock_tripped": safety.tripped_reason,
                "ledger": os.path.abspath(args.out),
                "ledger_durable_shot_id": ledger.durable_shot_id(),
                "audit": os.path.abspath(args.audit),
                "dry_run": True,
            }
            audit.record("hwctl.cli", "scan.end", args.out, summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        finally:
            ledger.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hwctl",
        description="nopper 硬件控制桥（rig/trigger/pwr/scope + 安全域互锁 + 台账/审计）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="delay/width 参数扫描（dry-run 全链可跑）")
    p_scan.add_argument("--grid", required=True, help="grid.json 路径（axes: name/unit/min/max/step）")
    p_scan.add_argument("--dry-run", action="store_true", help="Mock 全链，无硬件（C1 骨架唯一模式）")
    p_scan.add_argument("--out", default="scan-ledger.csv", help="CSV 台账输出路径")
    p_scan.add_argument("--audit", default="hwctl-audit.jsonl", help="审计日志输出路径")
    p_scan.add_argument("--safety", default=None, help="安全域 JSON（默认 ./safety-domain.json，缺省回退随包只读档）")
    p_scan.add_argument("--campaign", default=None, help="campaign_id（缺省取 grid.json）")
    p_scan.add_argument("--strategy", choices=["uniform", "adaptive3"], default=None, help="策略（缺省取 grid.json）")
    p_scan.add_argument("--batch-size", type=int, default=None, help="超参 hint：批大小 B（白名单 16-256）")
    p_scan.add_argument("--scope-sample-every", type=int, default=16, help="C4 抽样射间隔（1/16 缺省）")
    p_scan.add_argument("--match-bytes", default=DEFAULT_MATCH_BYTES, help="UartGuarder 匹配串（hex）")
    p_scan.set_defaults(func=cmd_scan)

    p_dev = sub.add_parser("devices", help="设备探测（--port COM19 对 power_shorter 做真实探测）")
    p_dev.add_argument("--port", default=None, help="power_shorter 的 COM 口（如 COM19）")
    p_dev.set_defaults(func=cmd_devices)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SafetyViolation as sv:
        print(f"[hwctl] 安全域拒绝: {sv}", file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print(f"[hwctl] 输入错误: {exc}", file=sys.stderr)
        return 1
