# nopper — 硬件安全研究多 Agent 编排框架

<p align="center"><img src="docs/assets/banner.jpg" alt="nopper — 硬件安全研究编排" width="860"></p>

**nopper** 是一个面向硬件安全研究（芯片故障注入、固件逆向、仪器化表征）的多 Agent 编排
harness。它不是单个 Agent，而是一支小车队：一个 AI 指挥官读板、派单、验收；工作者负责
接口表征、固件分析、毛刺注入与复现验证；零策略看门狗负责跨崩溃、跨上下文重置的保活。

全部共享状态是一张**事实图谱**：typed fact（13 类领域 kind）与 intent（实验工作单元）
构成的 DAG，一个节点一个 JSON 文件，文件锁保护——无数据库、无服务端、纯 Python 标准库。

> **来源说明。** nopper fork 自一个自主渗透竞赛框架（sapper-v2，某自主攻防挑战赛冠军方案）。
> 竞赛层（打分平台桥、bench 驱动、flag 提交管线）已删除；通用编排核心（事实图、dispatch
> 引擎、车队/看门狗、知识库）原样保留，本 fork 在其之上重建领域层。sapper 时代历史记录保留在
> `CHANGELOG.md` 与 `docs/`。

## 机制而非模型

nopper 不绑定任何 AI 模型或 CLI——每个角色的 Agent 与模型皆可替换：

| 杠杆 | 如何更换 |
|---|---|
| **Agent** | 任何兼容 Claude-Code 的 CLI（claude / codex / 自定义 `NOPPER_BIN`） |
| **模型** | `--model` 参数或 `NOPPER_MODEL` / `NOPPER_MODEL_FLASH` 环境变量 |
| **网关与密钥** | `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `NOPPER_KEY_POOL`（额度耗尽时看门狗自动轮换） |
| **预算** | `NOPPER_TOTAL_MINUTES`、每步 `--minutes`、`--deadline`、射击/能量/设备寿命预算 |
| **条令** | `docker/commander/SKILL.md` + `templates/*.md`（纯 markdown，改条令不改代码） |

**默认两档模型**（研究员拍板，GLM 管脑 / DeepSeek 管手）：

| 档位 | 模型 | 角色 |
|---|---|---|
| SMART | `zai-org/GLM-5.3`（`NOPPER_MODEL`） | 战役指挥官 / 固件分析师 / 知识管理员——调度、思考、抉择 |
| LINE | `deepseek/deepseek-v4.1-flash`（`NOPPER_MODEL_FLASH`） | 侦察分析员 / 毛刺操作员 / 验证员——干活量产 |

经同一网关（anthropic 协议，Claude Code 免登录直连），单网关无跨厂切换。

## 13 类事实图谱（实验语言）

`device`（被测设备）· `chip`（芯片）· `board`（板卡）· `interface`（调试接口）·
`firmware_image`（固件镜像）· `probe_point`（注入点，四问：注入点/窗口/触发/判定）·
`vulnerability`（可打点）· `glitch_param`（注入参数原子集）· `glitch_result`（实验结果，
outcome ∈ success/crash/no_effect/other）· `scan_campaign`（扫描战役）· `power_profile`
（供电画像）· `analysis_note`（分析结论，三级证据标注 observation/hypothesis/unknown）·
`repro_confirmed`（复现确认书）。

**台账**：实验结果走 finding 台账（observed → confirmed/rejected），复现升级由
verify-repro 判据驱动（泊松零计数 + 3σ + 基线三对照，源自真实工程实践）。

## 快速开始（WSL2 / Linux）

```bash
git clone https://github.com/Nop3z/nopper.git
cd nopper && bash install.sh        # 纯 Python 标准库，零依赖
nopper setup --goal "对某芯片做电压毛刺扫描" --agents
nopper status                       # 看板与就绪工作前沿
```

### 模型网关预置（Windows/WSL）

```bash
# 编辑 docker/setup-wsl-nopper.sh 填入网关地址与 key 后执行：
bash docker/setup-wsl-nopper.sh     # 写 env + 信任预置 + 双档冒烟
```

### 硬件桥（hwctl，Windows 侧）

设备控制与编排分离：**编排层在 WSL2（Linux），物理设备控制在 Windows（hwctl）**，
经 /mnt/d 文件交换（安全域配置 / 射击台账 / 审计日志）。

```powershell
python -m hwctl devices                        # 设备探测（真机只读探测）
python -m hwctl scan --grid grid.json --dry-run  # Mock 全链（无硬件验证）
```

已接入驱动：PowerShorter（电压短路毛刺，10ns 精度）/ UartGuarder（UART 字节串触发）/
pico3000（示波器）/ OSR_Smart_HUB（供电监测）/ Glasgow（USB 探测）。
安全域互锁（v_target 上限 / XOR 掩码校验 / 射击预算）Agent 不可写。

## 知识库（kb）

`kb/entries/` 一条一个 JSON：方法论 / 芯片家族 / 防护反制 / 工具台架 / 负知识五类，
带 `verified`（false=候选，人工复核后 true）与 `applicability`（适用芯片家族闸门，
防跨家族误迁移）字段。Agent 先查后干：`nopper kb search "电压毛刺"`。

## 安全与边界

- 仅供授权设备上的安全研究使用；物理动作唯一入口 = hwctl（安全域白名单，Agent 不可写）；
- 高危操作默认 `requires_confirmation`（首次上电/新轨道电压/新注入点需人工 confirm）；
- 互锁止损：连续 3 批全 crash → 供电回标称 + 人工复位确认才可续跑；
- 固件提取为研究员人工职责，Agent 只登记 sha256 与分析；
- 结论无证据链即无效：观测/假设/unknown 三级标注，静态↔仿真↔物理三方交叉验证才可升 confidence。

## 项目结构

```
nopper/
├── lib/nopper.py        # CLI + fact graph + dispatch 引擎（纯标准库）
├── hwctl/               # Windows 侧设备桥（安全域/台账/驱动 Mock+真机层）
├── templates/           # 七个角色模板（指挥官/实验/验证/扫描/…）
├── docker/commander/    # 条令 SKILL.md + worker.sh + watchdog.sh（车队运行时）
├── kb/                  # 知识库（100+ 条目）+ RE-REPORT 模板
├── tests/               # unittest（编排层）+ pytest（hwctl）
└── docs/                # 上游历史文档
```

## 许可与上游

MIT（见 LICENSE）。上游事实图谱核心来自开源社区贡献（fgc/claude-fact-graph 系）；
渗透竞赛时代的设计经验（sapper-v2，TSecBench 冠军方案）以历史文档形式致谢。
