#!/usr/bin/env bash
# nopper WSL2 环境预置（Phase 0 / D1）——填好两个尖括号值后执行：bash setup-wsl-nopper.sh
set -euo pipefail

# ===== 1. 模型网关（绕过 OAuth 登录的核心）=====
# 指挥官/智能档 = GLM；干活/便宜档 = DeepSeek-V4.1-Flash（经同一网关，模型名由网关决定）
GATEWAY_URL="https://api.commandcode.ai/provider"   # Command Code anthropic 网关（DSH cordis.patch.yml 同源）
GATEWAY_KEY="<COMMANDCODE_API_KEY，见 DSH .credentials.yaml refs 段>"  # ← 填这里

cat >> "$HOME/.bashrc" <<'EOF'

# ---- nopper gateway (added by setup-wsl-nopper.sh) ----
export ANTHROPIC_BASE_URL="__GW_URL__"
export ANTHROPIC_AUTH_TOKEN="__GW_KEY__"
export NOPPER_MODEL="zai-org/GLM-5.3"                 # 智能档（指挥官/固件分析师/知识管理员）
export NOPPER_MODEL_FLASH="deepseek/deepseek-v4.1-flash"  # 便宜档（侦察/毛刺操作员/验证员）
# 多 key 轮换池（JSON 数组，可选）：export NOPPER_KEY_POOL='[{"base_url":"...","token":"...","model":"..."}]'
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 DISABLE_AUTOUPDATER=1
EOF
# 写入真实值（heredoc 占位替换）
sed -i "s|__GW_URL__|${GATEWAY_URL}|; s|__GW_KEY__|${GATEWAY_KEY}|" "$HOME/.bashrc"

# ===== 2. 信任预置（免首启动对话框，等价上游 trust_preset）=====
mkdir -p "$HOME/.claude"
python3 - "$HOME" <<'PYTRUST'
import json, os, sys
path = os.path.expanduser("~/.claude.json")
try: d = json.load(open(path))
except Exception: d = {}
d.setdefault("hasCompletedOnboarding", True)
d["hasTrustDialogAccepted"] = True
d["bypassPermissionsModeAccepted"] = True
json.dump(d, open(path, "w"), indent=1)
PYTRUST
cat > "$HOME/.claude/settings.json" <<'JSON'
{"permissions":{"defaultMode":"bypassPermissions"},"autoCompactEnabled":true,"theme":"auto"}
JSON

# ===== 3. 冒烟验证（两档各一发）=====
echo "== 智能档冒烟（GLM）=="
printf 'reply with the single word: ok' | claude -p --model "$NOPPER_MODEL" && echo
echo "== 便宜档冒烟（DeepSeek）=="
printf 'reply with the single word: ok' | claude -p --model "$NOPPER_MODEL_FLASH" && echo
echo "== 完成。若两发都回 ok，WSL 车队环境验收通过 =="