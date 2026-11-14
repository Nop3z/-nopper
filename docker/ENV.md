# nopper 运行时环境变量清单（原 TSecBench 托管模式配置，遗留存档）

> 本文件原为渗透竞赛托管模式（王座协议线上赛）的配置清单。nopper fork 已移除
> 竞赛层（arena/bench），保留 docker/ 目录仅作服务器部署选项（D3 阶段处置）。
> 变量名已按 A7 统一改为 `NOPPER_*`；`BENCHMARK_*`（平台注入变量）仅在使用
> 遗留 docker/entrypoint.sh 驱动旧平台时才相关。

历史存档要点（2026-10-17 线上赛）：

- **只有 1 次评测机会**，仅队长账号可提交；Agent 异常没有重试
- 50 题 / 100 flag，任务限时 **8 小时**（镜像内默认按 470 分钟跑，留安全边际）
- 排名顺序：**得分 → 有效耗时 → token 量**（所以默认不开 verify 双检，省 token）
- 模型限流/超时/中断不算平台异常——镜像内已带**模型调用重试**
  （NOPPER_RETRY_MODEL=3，指数退避），单题异常自动跳过下一轮再试，绝不中断整个任务

## 一、模型网关（现役）

| KEY | VALUE | 说明 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `https://open.bigmodel.cn/api/anthropic` | 直连厂商公开端点 |
| `ANTHROPIC_AUTH_TOKEN` | 你的 API Key | 换供应商只改这项和 BASE_URL |
| `NOPPER_MODEL` | `glm-5.3` | 智能档（战役指挥官/固件分析师/知识管理员：调度思考抉择）。由 docker/setup-wsl-nopper.sh 预置 |
| `NOPPER_MODEL_FLASH` | `deepseek-v4.1-flash` | 便宜档（侦察分析员/毛刺操作员/验证员：干活量产），同一网关 |

### Key 池自动轮换（强烈建议配置）

当某个 Key 额度耗尽/401/欠费，系统自动切换到池中下一组凭据，`-p` 版**无需重启**（下一次模型调用即用新 Key），tmux 版自动轮换并重启对应 TUI（`claude --continue` 恢复原会话上下文）。检测特征：insufficient/balance/quota/exceeded/billing/401/unauthorized/欠费/余额。

| KEY | VALUE 格式 |
|---|---|
| `NOPPER_KEY_POOL` | `[{"base_url":"<网关地址>","token":"key1","model":"glm-5.3"},{"base_url":"<网关地址>","token":"key2","model":"deepseek-v4.1-flash"}]` | 同网关两档模型的 key 池（watchdog 轮换） |

注意：池里各组 URL 直连各厂商公开端点。单 Key 时轮换自动退化为普通重试。

## 二、nopper 运行时变量（现役，不填用默认值）

| KEY | 默认 | 说明 |
|---|---|---|
| `NOPPER_ROOT` | `/work/runs` | 车队根目录（每个实验一张子图） |
| `NOPPER_TOTAL_MINUTES` | `470` | 全局死线（watchdog .deadline 依据） |
| `NOPPER_MINUTES_PER_CHALLENGE` | `9` | 每个实验每轮 auto 分钟数（遗留变量名，语义=per-campaign） |
| `NOPPER_MAX_STEPS` | `10` | 每轮最多 reason 轮数 |
| `NOPPER_RETRY_MODEL` | `3` | 模型调用失败重试次数（10s/20s/40s 退避） |
| `NOPPER_MAX_WORKERS` | `8` | worker 窗格上限（exit 4 = 满） |
| `NOPPER_STALL_SECONDS` | `600` | 看门狗停滞观察窗（故意不干预，仅记录） |
| `NOPPER_KB_ROOT` | 随安装 | 离线知识库位置 |
| `NOPPER_BIN` | `nopper` | 引擎命令前缀（须输出 {"result":...} 信封） |
| `NOPPER_VERIFY` | 不设 | 设为 `1` 才开启核验者（费 token） |

## 三、部署自查（docker 部署选项）

- [ ] 镜像内 `nopper --version` 正常
- [ ] `ANTHROPIC_*` 变量填的是厂商公开端点地址 + 正确 key
- [ ] 密钥全走环境变量，镜像不含任何密钥
- [ ] WSL2 原生部署可完全跳过本目录（D3）
