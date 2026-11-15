# hwctl —— nopper 硬件控制桥（C1 骨架）

fork-plan §8 三层桥的桥接层：Agent 只见 hwctl 受控接口；pyvisa/pyserial/hidapi
藏在 drivers 后面。安全域互锁 + CSV 台账 + 审计日志都在本层。

## 模块地图

| 模块 | 职责 | 依据 |
|---|---|---|
| protocol.py | RigDriver/PwrDriver/ScopeDriver/TriggerDriver 四 Protocol + InjectionReceipt（pattern 原样/unit_scale/confidence/时间戳）+ scan 六件套 | tool-integration §2.1/§2.2 + fi-integration §2.7 |
| safety.py | SafetyDomain（v_target/pattern 总时长/能量/连发 crash/射击预算，JSON 只读加载）+ 越界拒绝落审计 + XOR 掩码校验 | fork-plan §8、fi-integration §2.7（FI 教训） |
| ledger.py | ShotLedger 异步 CSV 台账（批尾 flush+fsync、flush_before(fact_id) 铁律）+ AuditLog（JSONL 逐条写透） | scan-acceleration §3.2 #8 |
| drivers/ | powershorter（FI 语义包装）/ uart_guarder / pico3000（六件套）/ shub；全部 Mock 行为等价，真机层 from_port()/from_serial() 延迟 import | tool-integration §1/§5.1 |
| scan/ | params（{axis}_{unit} 值对象 + 超参白名单 §2.5）/ strategy（uniform 完整 + adaptive3 骨架）/ executor（单射状态机+复位分档）/ judge（C1/C2/C3[+C5] + outcome + heat_score） | scan-acceleration §2/§3/§4/§6.2 |
| cli.py | hwctl scan --grid grid.json --dry-run 全链 + hwctl devices | MVP 最小集 |

## 铁律

1. 安全域配置文件对 Agent **只读**（本包无任何写回接口）；越界即拒绝并落审计。
2. XOR 掩码不变量：bit_mask_on_reg_value 必须 == source XOR target（FI 教训）。
3. 结果 fact 落图前，台账必须已 flush + fsync（ShotLedger.flush_before /
   ShotExecutor.ensure_flushed）。
4. 参数永远由策略器代码生成；模型 Agent 只能通过 hint 改超参白名单六项
   （HyperParams 范围校验 + 审计）。
5. 复位分档：常规射 RESET 脉冲；仅 crash 才 0V 断电（power_cycle 300ms）。

## 跑通

仓库根目录（Windows Python 3.12）：

    python -m hwctl scan --grid grid.json --dry-run
    python -m hwctl devices
    python -m pytest tests -q

grid.json 形状：

    {
      "axes": [
        {"name": "delay", "unit": "raw", "min": 0, "max": 900, "step": 100},
        {"name": "pulse", "unit": "raw", "min": 0, "max": 8,   "step": 2}
      ],
      "strategy": "uniform",
      "campaign_id": "demo-001"
    }

## Mock 与真机的行为差异（C1 骨架声明）

| 侧 | Mock | 真机（TODO） |
|---|---|---|
| 时序 | 时序参数可缩放为 0（链路等价不睡真时间）；真机按 §3.2 量级（RESET 10-50ms / 0V 300ms） | 实机校准 TODO(P0) |
| 注入 | soft_trigger 软触发路径；engine_cfg 校验等价（<=8 段/level 0-1/dur>=0） | UartGuarder TRI -> trigger_edges 硬件同源触发（v1） |
| 目标行为 | uart mock 注入假应答/假崩溃（normal/mutated/silent_crash）+ shub 负载模型（崩板电流掉底） | 真目标 UART/电流；崩溃不可脚本化 |
| 判定 | ping RTT 固定值、C3 单点读近似；判定规则与真机一致 | 超时自适应（p99x1.5）、10-50ms 轮询曲线（P0） |
| C4 | scope 合成波形只存证，特征列留空 | pico3000 塌陷特征提取（v1） |
| 供电 | shub mock 无真实 V/I 与 hidapi 独占约束 | hidapi 单进程互斥；逐端口断电止损（P1） |

真机层入口：PowerShorterRig.from_port / UartGuarderTrigger.from_port /
Pico3000Scope.from_serial / SmartHubPwr.from_serial（延迟 import，无设备环境
不触发；继电器极性等电气参数待人工校验——tool-integration §5.3）。
