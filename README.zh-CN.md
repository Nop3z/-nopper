# nopper — 硬件安全研究编排框架

<p align="center"><img src="docs/assets/banner.jpg" alt="nopper — 硬件安全研究编排" width="860"></p>

**nopper** 是一个面向硬件安全研究（芯片故障注入、固件逆向、仪器化表征）的
多 Agent 编排 harness。它不是单个 Agent，而是一支小车队：一个 AI 指挥官读板、
派单、验收；工作者负责接口表征、固件提取、毛刺注入与复现验证；零策略看门狗
负责跨崩溃、跨上下文重置的保活。

全部共享状态是一张**事实图谱**：typed fact（device/chip/board/interface/
firmware_image/glitch_param/glitch_result/scan_campaign/power_profile 等 13 类）
与 intent（实验工作单元）构成的 DAG，一个节点一个 JSON 文件，文件锁保护——
无数据库、无服务端、纯 Python 标准库。

> **来源说明。** nopper fork 自一个自主渗透竞赛框架（sapper-v2，某自主
> 攻防挑战赛冠军方案）。竞赛层——打分平台桥、bench 驱动、flag 提交管线——
> 已在本 fork 中删除；通用编排核心（事实图、dispatch 引擎、车队/看门狗、
> 知识库）原样保留，本 fork 在其之上重建领域层。sapper 时代的历史记录保留在
> `CHANGELOG.md` 与 `docs/`。

## 机制而非模型

nopper 不绑定任何 AI 模型或 CLI——每个角色的 Agent 与模型皆可替换：

| 杠杆 | 如何更换 |
|---|---|
| **Agent** | 任何兼容 Claude-Code 的 CLI（claude / codex / 自定义 `NOPPER_BIN`） |
| **模型** | `--model` 参数或 `NOPPER_MODEL` / `NOPPER_MODEL_FLASH` 环境变量 |
| **网关与密钥** | `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `NOPPER_KEY_POOL`（额度耗尽时看门狗自动轮换） |
| **预算** | `NOPPER_TOTAL_MINUTES`、每步 `--minutes`、`--deadline` |
| **条令** | `docker/commander/SKILL.md` + `templates/*.md`（纯 markdown） |
| **fact 类型** | `lib/nopper.py` 的 13 类领域 kind |

nopper 的价值不在任何单个 Agent，而在**组织多个 Agent 协同的机制**：
调度、分工、通信、记忆、自愈——换掉所有 Agent 和模型，这套机制照常运转。

## 快速开始（WSL2 / Linux）

```bash
bash install.sh                       # 符号链接 nopper CLI（纯标准库，零依赖）
cd 你的实验项目
nopper setup --goal "表征目标板，定位毛刺窗口" --agents
nopper fact "PA13/PA14 确认 SWD 接口" -k interface -d chip=stm32f103 -d evidence_level=observation -d confidence=high
nopper finding "ext_offset=47 width=32 skip" -c c01    # finding 台账（去重、先记后验）
nopper dispatch explore i001          # 单步执行
nopper auto --minutes 60              # reason -> explore -> verify 循环至完成
nopper validate                       # 图审计（含 evidence 元数据软告警）
nopper view                           # 自包含 HTML DAG
```

tmux 车队（指挥官 + 工作者 + 看门狗）见 `docker/commander/SPEC.md` 与
`docker/commander/SKILL.md`；车队需在 WSL2/Linux 内原生运行（依赖 tmux + fcntl）。

## Findings，不是 flags

台账记录的是**实验观测结果**——按 (campaign, value) 去重、先记录后确认的
崩溃安全设计、三态生命周期：`observed` -> `confirmed`（重放统计达标）或
`rejected`。知识沉淀在离线 kb 中（`nopper kb search "voltage glitch"`）。

## 安全与边界

硬件实验作用于物理世界：请阅读 `DISCLAIMER.md`，只在自有或获授权的设备上
实验，把条令中的安全包络（电压上限、复位互锁）当作硬规则而非建议。

## 许可与上游

MIT。上游通用底座：[nvwabase · 女娲基座](https://github.com/N1nEmAn/nvwabase)
（造人 / 语言 / 历史三能力）。详见 `LICENSE` 与仓库历史中的上游信息。
