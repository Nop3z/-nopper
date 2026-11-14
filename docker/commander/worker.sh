#!/usr/bin/env bash
# worker.sh v1 — the commander's hands: one-shot subcommands, no main loop.
#
#   - platform cap handling removed with the arena bridge (nopper has no
#     competition platform; exit code 3 is no longer produced here)
#     crashes the caller (inherited trap: "arena HTTP 409 must never crash")
#   - worker pane cap                       -> live window count vs
#     NOPPER_MAX_WORKERS enforced before any platform call, exit 4 WORKER CAP
#   - trust/bypass/permission dialogs       -> per-project hasTrustDialogAccepted
#     preset + Down+Enter auto-accept while waiting for the input box
#   - bracketed paste breaks ink TUIs       -> send-keys -l ONLY, never
#     load-buffer/paste-buffer
#   - mission stuck unsubmitted in input box-> confirm text in pane, retry once
#   - pane death detection                  -> every worker window ends with
#     "echo DRIVE_PANE_EXITED; exec sleep infinity" (list/board/watchdog read it)
#   - same-cwd --continue cross-talk        -> one dir per agent; -w2+ mounts the
#     shared graph via a .nopper symlink instead of a second graph
#   - addrs file kept for output compatibility only (no platform in nopper;
#     spawn no longer writes ROOT/<code>/.addrs)
#   - rotated gateway keys (watchdog)       -> spawn syncs the tmux SESSION env so
#     new panes inherit the CURRENT key, not the server-startup key (tmux only
#     copies update-environment vars from the client — ANTHROPIC_* is not in it)
#   - repeated calls                        -> idempotent: kill-window before
#     create, guarded nopper init, next free -wN index derived from live panes
set -o pipefail

ROOT="${NOPPER_ROOT:-/work/runs}"
LOG="$ROOT/drive.log"
# Model tiers & gateways (SKILL 4.9, final doctrine): ALL-IN FLAGSHIP.
#   glm gw + SMART (glm-5.3[1m]) is the default brain for EVERY worker — soldier,
#   scout, chore, general alike. flash is retired from this deployment.
#   ds gw (deepseek-flash) is the RESERVE army: takes over when glm quota/
#   rate limits bite (the watchdog rotates; the commander may also spawn
#   --gw ds deliberately under quota pressure).
# mixed doctrine: soldiers default to the ds tier (cheap mass), generals and
# the commander run NOPPER_MODEL (the strong brain) — the general-escalation
# below fires when model==MODEL, which holds for this default.
# NOTE: inline NOPPER_MODEL_DS (DS_MODEL is defined LATER in this header —
# referencing it here silently fell back to glm and killed the ds army).
MODEL="${NOPPER_MODEL_DS:-${NOPPER_MODEL:-glm-5.3[1m]}}"
DS_MODEL="${NOPPER_MODEL_DS:-deepseek-flash}"
DS_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.deepseek.com/anthropic}"
DS_KEY="${DEEPSEEK_API_KEY:-}"
SESSION=nopper-cmd
MAX_WORKERS="${NOPPER_MAX_WORKERS:-8}"
DEF_MINUTES=10
NOPPER="${NOPPER_BIN:-nopper}"
ACT_FILE="$ROOT/.worker-activity.tsv"
PANE_ENV_DIR="$ROOT/pane-env"
NOW() { date +%s; }
mkdir -p "$ROOT" "$PANE_ENV_DIR"

log() { printf '[worker] %s\n' "$*" | tee -a "$LOG" >&2; }
die() { local rc=$1; shift; printf '[worker] ERROR: %s\n' "$*" >&2; exit "$rc"; }

is_code() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ ]]; }
window_exists() { tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -qx -- "$1"; }

# ---- findings ledger stub (competition submission removed with the arena
#      bridge; the nopper finding ledger records results as facts and
#      verify-repro upgrades them - observed/confirmed/rejected) ------------
finish_flags() {  # finish_flags <code> - kept for call-site compatibility
  local code=$1
  log "finish_flags: no submission target (nopper records only) - findings stay as recorded facts for $code"
}


# ---- pane primitives (verbatim from tmux-drive.sh) ---------------------------
pane_text() { tmux capture-pane -pt "$SESSION:$1" 2>/dev/null; }

send_literal() {  # send_literal <window> <text> [enter] — NEVER bracketed paste
  tmux send-keys -t "$SESSION:$1" -l -- "$2"
  sleep 0.5
  [[ "${3:-enter}" == "enter" ]] && tmux send-keys -t "$SESSION:$1" Enter
}

accept_dialogs() {  # accept_dialogs <window> <pane-text> -> 0 if a dialog was dismissed
  local win="$1" pane="$2"
  if printf '%s' "$pane" | grep -qE 'Enter to confirm|Yes, I accept|I accept'; then
    # Aim by the ❯ focus marker: security dialogs default-focus "No, exit",
    # so a bare Enter CHOOSES NO. Only move+confirm when focus is off Yes.
    if printf '%s' "$pane" | grep -qE '❯[^\n]*No, exit'; then
      tmux send-keys -t "$SESSION:$win" Down; sleep 0.4
    fi
    tmux send-keys -t "$SESSION:$win" Enter; sleep 1.5
    return 0
  fi
  return 1
}

# ---- worker-pane registry ----------------------------------------------------
worker_windows() {  # every window except the commander's window 0
  tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -vx commander || true
}
count_workers() {
  local n; n=$(worker_windows | grep -c . || true)
  printf '%s' "${n:-0}"
}
panes_for_code() {  # panes_for_code <code>
  local re=${1//./\\.}
  worker_windows | grep -E "^${re}-w[0-9]+$" || true
}

sync_session_env() {  # tmux new-window inherits the SESSION environment, not the
                      # calling client's env (only update-environment vars are
                      # copied). Push the live gateway/platform env in so a key
                      # rotated by the watchdog reaches freshly spawned panes;
                      # PATH so the panes see the same tooling as the commander.
  local k v
  for k in PATH ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN NOPPER_MODEL NOPPER_MODEL_FLASH; do
    [[ -n "$v" ]] && tmux set-environment -t "$SESSION" "$k" "$v" >/dev/null 2>&1 || true
  done
}

# ---- activity tracking (worker.sh is one-shot; state lives on disk) ----------
declare -A OLD_TS OLD_SHA NEW_TS NEW_SHA
collect_panes() {  # prints TSV: name <TAB> alive|exited <TAB> idle <TAB> since <TAB> WxH
  OLD_TS=(); OLD_SHA=(); NEW_TS=(); NEW_SHA=()
  local w ts h text st since now; now=$(NOW)
  if [[ -f "$ACT_FILE" ]]; then
    while IFS=$'\t' read -r w ts h; do OLD_TS[$w]=$ts; OLD_SHA[$w]=$h; done < "$ACT_FILE"
  fi
  while IFS='|' read -r w created cols rows; do
    [[ -n "$w" ]] || continue
    [[ "$w" == "commander" ]] && continue   # window 0 is the commander, not a worker
    text=$(pane_text "$w")
    if printf '%s' "$text" | grep -q 'DRIVE_PANE_EXITED'; then st=exited; else st=alive; fi
    h=$(printf '%s' "$text" | sha256sum | cut -c1-16)
    if [[ "${OLD_SHA[$w]:-}" == "$h" && -n "${OLD_TS[$w]:-}" ]]; then ts=${OLD_TS[$w]}; else ts=$now; fi
    NEW_TS[$w]=$ts; NEW_SHA[$w]=$h
    since=$(date -d "@${created:-$now}" +%H:%M 2>/dev/null || echo '?')
    printf '%s\t%s\t%s\t%s\t%sx%s\n' "$w" "$st" "$(( now - ts ))" "$since" "${cols:-?}" "${rows:-?}"
  done < <(tmux list-windows -t "$SESSION" -F '#{window_name}|#{window_created}|#{window_width}|#{window_height}' 2>/dev/null)
  local tmp; tmp=$(mktemp) || return 0
  : > "$tmp"
  for w in "${!NEW_TS[@]}"; do
    printf '%s\t%s\t%s\n' "$w" "${NEW_TS[$w]}" "${NEW_SHA[$w]}" >> "$tmp"
  done
  mv "$tmp" "$ACT_FILE"   # atomic; entries for dead panes are pruned
}

# ---- trust preset (verbatim from tmux-drive.sh launch()) ----------------------
trust_preset() {  # trust_preset <dir>
  local dir=$1
  mkdir -p "$dir/.claude"
  printf '%s\n' '{"permissions":{"defaultMode":"bypassPermissions"},"autoCompactEnabled":true}' > "$dir/.claude/settings.json"
  python3 - "$dir" <<'PYTRUST'
import json, os, sys
path = os.path.expanduser("~/.claude.json")
try: d = json.load(open(path))
except Exception: d = {}
d.setdefault("projects", {})[sys.argv[1]] = {
    "hasTrustDialogAccepted": True, "bypassPermissionsModeAccepted": True}
json.dump(d, open(path, "w"))
PYTRUST
}

ensure_graph() {  # ensure_graph <code> — guarded nopper init in the primary dir
  local code=$1
  [[ -f "$ROOT/$code/.nopper/project.json" ]] && return 0
  mkdir -p "$ROOT/$code"
  # $NOPPER is intentionally unquoted: NOPPER_BIN may carry its own args
  (cd "$ROOT/$code" && $NOPPER init --goal "experiment $code: characterize the target and record findings" >/dev/null 2>&1 || true)
}

write_hint() {  # write_hint <code> <text> — appends a [commander]-prefixed hint fact
  local code=$1 text=$2 store="$ROOT/$1/.nopper" hid
  ensure_graph "$code"
  hid=$($NOPPER --store "$store" hint "[commander] $text" --creator commander 2>&1) \
    || die 1 "hint: nopper hint failed for $code: $hid"
  log "hint -> $code: $hid (${text:0:50})"
  printf 'HINT %s -> %s\n' "$hid" "$code"
}

close_instance() {  # close_instance <code> - no platform in nopper: findings stay
  local code=$1
  log "$code: no platform instance to close (arena removed)"
  finish_flags "$code" 2>&1 | tee -a "$LOG" >&2 || true
  rm -f "$ROOT/$code/.addrs"
}

general_mission() {  # general_mission <code> <addrs> — smart worker allowed to hire -p squads
  local code=$1 addrs=$2
  printf '%s' "You are a GENERAL (senior analyst) on experiment $code. Think DEEPLY — you have the biggest brain on this experiment, use it: plan the full analysis chain before executing, reason step by step. Targets: ${addrs}. You solve the experiment yourself AND you may hire one-shot squads for bounded chores: run /opt/nopper/docker/commander/worker.sh spawn $code --p --minutes 5 --mission \"<chore: interface sweep / extract one firmware region / verify a guess>\" — the squad runs ~5 min, prints results in its pane, then goes idle: peek its pane (worker.sh peek <pane> 120), harvest, and kill it (worker.sh kill <pane>). Squads share this experiment's fact graph — tell them to read it first. Caps: panes are limited (a spawn failing with exit 4 means wait or fire a squad); keep at most 2 squads at a time; never let a squad idle long after it finishes. Record findings as facts, record EVERY observed result and IMMEDIATELY run nopper finding \"<result token>\" -c $code. Communication with your commander: [HELP]/[REPORT] notes in the graph as usual. Work fully autonomously; reply DONE only when the experiment is finished."
}

default_mission() {  # default_mission <code> <addrs> <primary 0|1>
                     # operator prompt carried over from tmux-drive.sh launch()
  local code=$1 addrs=$2 primary=$3 extra=""
  (( ! primary )) && extra=" Another agent is also working $code from a sibling directory — you SHARE one fact graph: read the existing facts before repeating work, and record every new finding with nopper fact so your partner sees it."
  printf '%s' "You are an autonomous experiment operator in a nopper hardware-research fleet. Think DEEPLY before acting: plan multi-step measurements carefully, reason about what each command's output tells you before the next one — depth of thought is your main weapon. Experiment: $code. Rig/bench addresses (reach them directly): ${addrs}. Mission: characterize the target (device/chip/board/interface facts), extract or analyze what the intent asks for, record EVERY observed result, and for each success IMMEDIATELY run: nopper finding \"<result token>\" -c $code  (this records the result in the finding ledger crash-safe). Record findings as typed facts: nopper fact \"...\" -k interface -d chip=... -d pins=...  Consult the knowledge base when unsure: nopper kb search \"keywords\". Multiple results are likely — after one success keep hunting the rest of the intent. Communication: your commander reads this experiment's fact graph every ~2 minutes — to reach it, record a fact: nopper fact \"[HELP] <exactly what you tried, what blocks you, what decision you need>\" -k analysis_note (only when GENUINELY blocked, max one open HELP at a time; keep working alternatives while waiting) or \"[REPORT] <significant intel>\" -k analysis_note (interesting findings your commander may relay to other experiments). Do not wait idle for replies. When totally done, reply with the single word DONE.$extra"
}

# ---- spawn -------------------------------------------------------------------
cmd_spawn() {
  local code="" mode=tui mission="" minutes=$DEF_MINUTES name="" model="" gw="" engine=claude general=0 a
  while (( $# )); do
    a=$1; shift
    case "$a" in
      --tui) mode=tui ;;
      --p|-p) mode=p ;;
      --general) general=1 ;;
      --gw) [[ $# -gt 0 ]] || die 1 "spawn: --gw needs a value (glm|ds)"; gw=$1; shift ;;
      --gw=*) gw=${a#*=} ;;
      --engine) [[ $# -gt 0 ]] || die 1 "spawn: --engine needs a value (claude|codex)"; engine=$1; shift ;;
      --engine=*) engine=${a#*=} ;;
      --mission) [[ $# -gt 0 ]] || die 1 "spawn: --mission needs a value"; mission=$1; shift ;;
      --mission=*) mission=${a#*=} ;;
      --minutes) [[ $# -gt 0 ]] || die 1 "spawn: --minutes needs a value"; minutes=$1; shift ;;
      --minutes=*) minutes=${a#*=} ;;
      --name) [[ $# -gt 0 ]] || die 1 "spawn: --name needs a value"; name=$1; shift ;;
      --name=*) name=${a#*=} ;;
      --model) [[ $# -gt 0 ]] || die 1 "spawn: --model needs a value"; model=$1; shift ;;
      --model=*) model=${a#*=} ;;
      -*) die 1 "spawn: unknown option $a" ;;
      *) [[ -z "$code" ]] || die 1 "spawn: unexpected argument $a"; code=$a ;;
    esac
  done
  [[ -z "$gw" || "$gw" == glm || "$gw" == ds ]] || die 1 "spawn: --gw must be glm or ds (got '$gw')"
  [[ "$engine" == claude || "$engine" == codex ]] || die 1 "spawn: --engine must be claude or codex"
  [[ -n "$code" ]] || die 1 "usage: worker.sh spawn <code> [--tui|--p] [--mission \"...\"] [--minutes N] [--name NAME] [--model <id>] [--gw glm|ds] [--engine claude|codex]"
  [[ "$gw" == ds && -z "$DS_KEY" && "$engine" == claude ]] \
    && die 1 "spawn: --gw ds needs DEEPSEEK_API_KEY (unset — ask the operator, do not retry)"
  # default model per gateway: glm->flash tier, ds->deepseek-flash;
  # a GENERAL defaults to the smart tier (SKILL 4.10). NOTE: compare with
  # quoted `==` — unquoted/glob paths treat the [1m] suffix as a char class
  # and silently never match.
  [[ -n "$model" ]] || { [[ "$gw" == ds ]] && model="$DS_MODEL" || model="$MODEL"; }
  if (( general )) && [[ "$gw" == glm && "$model" == "$MODEL" ]]; then
    model="${NOPPER_MODEL:-glm-5.3[1m]}"
  fi
  # gateway auto-derives from the FINAL model's family (mixed doctrine: the
  # default soldier model is deepseek — its name on the glm gateway 404s,
  # which killed every soldier pane in a live run). Explicit --gw wins.
  [[ -z "$gw" ]] && { case "$model" in *deepseek*) gw=ds;; *) gw=glm;; esac; }
  is_code "$code" || die 1 "spawn: invalid code '$code'"
  [[ "$minutes" =~ ^[0-9]+$ ]] || die 1 "spawn: --minutes must be an integer (got '$minutes')"
  tmux has-session -t "$SESSION" 2>/dev/null \
    || die 1 "tmux session '$SESSION' missing (entrypoint/watchdog owns it)"

  # hard worker cap BEFORE touching the platform (an instance slot is precious)
  local nw; nw=$(count_workers)
  if (( nw >= MAX_WORKERS )); then
    die 4 "WORKER CAP: $nw/$MAX_WORKERS worker panes live — kill one first (worker.sh kill <pane>)"
  fi

  local existing; existing=$(panes_for_code "$code")
  local max=0 n w
  for w in $existing; do
    [[ "$w" =~ ^${code//./\\.}-w([0-9]+)$ ]] && { n=${BASH_REMATCH[1]}; (( n > max )) && max=$n; }
  done
  local ordinal=$(( max + 1 ))
  local pane
  if [[ -n "$name" ]]; then
    pane=$name
    [[ "$pane" =~ ^[A-Za-z0-9_-]+$ ]] || die 1 "spawn: --name must be [A-Za-z0-9_-] (tmux target parsing)"
    if window_exists "$pane"; then
      # re-spawning one of THIS code's own worker panes is allowed (idempotent);
      # colliding with a foreign window is not
      [[ "$pane" =~ ^${code//./\\.}-w[0-9]+$ ]] \
        || die 1 "spawn: window '$pane' already exists (belongs to another pane)"
    fi
  else
    pane="${code}-w${ordinal}"
  fi

  # primary agent = first pane of this code; it owns ROOT/<code>; -w2+ get their
  # own dir with the shared graph symlinked in (no same-cwd --continue crosstalk)
  local primary=0 dir="$ROOT/$code"
  (( max == 0 )) && primary=1
  (( primary )) || dir="$ROOT/${code}-w${ordinal}"

  # 1) no platform in nopper - no instance acquisition; addrs stay empty
  local addrs=""

  # 2) dirs + shared graph
  if (( primary )); then
    mkdir -p "$dir"
    ensure_graph "$code"
  else
    ensure_graph "$code"
    mkdir -p "$dir"
    ln -sfn "../$code/.nopper" "$dir/.nopper"
  fi
  # 3) trust preset
  trust_preset "$dir"

  # 4/5) the window (NO --continue: fresh session per pane; watchdog owns recovery)
  sync_session_env
  tmux kill-window -t "$SESSION:$pane" 2>/dev/null
  # persist the pane's tier+gateway so watchdog relaunches keep the same brain.
  # RAW values, never %q: bash %q escapes the [1m] suffix -> '\\[1m\\' and
  # pane_model()'s sed would feed claude a backslashed model id. TUI missions
  # are archived base64 so the watchdog can re-brief a restarted pane (a
  # relaunched claude is a FRESH session — nobody else will send its mission).
  local mission_b64=""
  [[ "$mode" == tui && "$engine" == claude && -n "$mission" ]] \
    && mission_b64=$(printf '%s' "$mission" | base64 -w0)
  printf 'MODEL=%s\nGW=%s\nENGINE=%s\nMISSION_B64=%s\n' \
    "$model" "$gw" "$engine" "$mission_b64" > "$PANE_ENV_DIR/$pane.env"
  # per-pane gateway env: ds panes override ANTHROPIC_* with the deepseek endpoint
  local gw_env=""
  [[ "$gw" == ds ]] && gw_env="ANTHROPIC_BASE_URL='$DS_BASE_URL' ANTHROPIC_AUTH_TOKEN='$DS_KEY'"
  if [[ "$mode" == tui ]]; then
    if [[ "$engine" == codex ]]; then
      # codex 0.42: provider via -c overrides on top of ~/.codex/config.toml
      # (chat wire for DeepSeek). ds-local = official endpoint for local sims.
      local provider=deepseek; [[ "$DS_BASE_URL" == https://api.deepseek.com* ]] && provider=deepseek-local
      tmux new-window -d -t "$SESSION" -n "$pane" \
        "cd $dir && DEEPSEEK_API_KEY='$DS_KEY' codex --skip-git-repo-check -c model_provider=$provider -c model='$model' --dangerously-bypass-approvals-and-sandbox 2>&1; echo DRIVE_PANE_EXITED; exec sleep infinity"
    else
      tmux new-window -d -t "$SESSION" -n "$pane" \
        "cd $dir && $gw_env claude --model '$model' --effort max --dangerously-skip-permissions 2>&1; echo DRIVE_PANE_EXITED; exec sleep infinity"
    fi
  else
    if [[ "$engine" == codex ]]; then
      [[ -n "$mission" ]] || mission=$(default_mission "$code" "$addrs" "$primary")
      local esc_mission=${mission//\'/\\'\'}
      tmux new-window -d -t "$SESSION" -n "$pane" \
        "cd $dir && DEEPSEEK_API_KEY='$DS_KEY' codex exec --skip-git-repo-check -c model_provider=deepseek-local -c model='$model' --dangerously-bypass-approvals-and-sandbox '$esc_mission' 2>&1; echo DRIVE_PANE_EXITED; exec sleep infinity"
      printf 'SPAWNED %s addrs=%s model=%s engine=codex\n' "$pane" "$addrs" "$model"
      return 0
    else
      tmux new-window -d -t "$SESSION" -n "$pane" \
        "cd $dir && $gw_env NOPPER_MODEL='$model' $NOPPER --store $ROOT/$code/.nopper auto --minutes $minutes --skip-permissions 2>&1; echo DRIVE_PANE_EXITED; exec sleep infinity"
    fi
  fi

  # 6) mission
  if [[ "$mode" == tui && "$engine" == codex ]]; then
    # codex TUI readiness: no claude-style ❯ marker; wait for the process to
    # settle, then type the mission (codex accepts typed input at its prompt)
    sleep 12
    [[ -z "$mission" ]] && mission=$(default_mission "$code" "$addrs" "$primary")
    send_literal "$pane" "$mission"
    log "$pane: codex TUI spawned (dir=$dir), mission sent"
  elif [[ "$mode" == tui ]]; then
    local waited=0 ptext="" frag
    while (( waited < 75 )); do
      ptext=$(pane_text "$pane")
      accept_dialogs "$pane" "$ptext" && { waited=$((waited+3)); continue; }
      printf '%s' "$ptext" | grep -q '❯' && break
      sleep 1; waited=$((waited+1))
    done
    printf '%s' "$ptext" | grep -q '❯' \
      || die 1 "spawn: $pane input box never appeared after 75s (pane left for inspection)"
    if [[ -z "$mission" ]]; then
      if (( general )); then mission=$(general_mission "$code" "$addrs")
      else mission=$(default_mission "$code" "$addrs" "$primary"); fi
    fi
    send_literal "$pane" "$mission"
    sleep 3
    ptext=$(pane_text "$pane")
    frag=${mission:0:24}
    if ! printf '%s' "$ptext" | grep -qF -- "$frag" \
       && ! printf '%s' "$ptext" | grep -q 'esc to interrupt'; then
      log "$pane: mission not confirmed in pane — retrying once"
      send_literal "$pane" "$mission"
      sleep 3
    fi
    log "$pane: TUI spawned (dir=$dir), mission sent"
  else
    sleep 2
    if printf '%s' "$(pane_text "$pane")" | grep -q 'DRIVE_PANE_EXITED'; then
      die 1 "spawn: $pane -p command exited immediately — check the nopper invocation in the pane"
    fi
    [[ -n "$mission" ]] && write_hint "$code" "$mission"
    log "$pane: -p worker spawned (dir=$dir, minutes=$minutes)"
  fi

  # 7)
  printf 'SPAWNED %s addrs=%s model=%s\n' "$pane" "$addrs" "$model"
}

# ---- kill --------------------------------------------------------------------
cmd_kill() {
  local close=0 target="" a
  while (( $# )); do
    a=$1; shift
    case "$a" in
      --close) close=1 ;;
      -*) die 1 "kill: unknown option $a" ;;
      *) [[ -z "$target" ]] || die 1 "kill: unexpected argument $a"; target=$a ;;
    esac
  done
  [[ -n "$target" ]] || die 1 "usage: worker.sh kill <pane>|<code> [--close]"
  tmux has-session -t "$SESSION" 2>/dev/null \
    || die 1 "tmux session '$SESSION' missing (entrypoint/watchdog owns it)"

  local re=${target//./\\.} wins
  if window_exists "$target"; then
    wins=$target
  else
    wins=$(worker_windows | grep -E "^${re}-w[0-9]+$" || true)
  fi
  [[ -n "$wins" ]] || die 1 "kill: no pane matches '$target'"

  local code=$target w
  [[ "$code" =~ -w[0-9]+$ ]] && code=${code%-w*}
  for w in $wins; do
    if tmux kill-window -t "$SESSION:$w" 2>/dev/null; then
      log "killed $w"
    else
      log "kill: window $w already gone"
    fi
    rm -f "$PANE_ENV_DIR/$w.env"
  done

  if (( close )); then
    local left; left=$(panes_for_code "$code")
    if [[ -n "$left" ]]; then
      log "$code: sibling panes still alive ($left) — instance kept open, findings not touched"
      return 0
    fi
    close_instance "$code"
  fi
}

# ---- msg / peek / list -------------------------------------------------------
cmd_msg() {
  local pane=${1:-} text=${2:-}
  [[ -n "$pane" && -n "$text" ]] || die 1 'usage: worker.sh msg <pane> "<text>"'
  tmux has-session -t "$SESSION" 2>/dev/null || die 1 "tmux session '$SESSION' missing"
  window_exists "$pane" || die 1 "msg: no such pane '$pane'"
  send_literal "$pane" "$text"
  log "msg -> $pane: ${text:0:60}"
}

cmd_peek() {
  local pane=${1:-} lines=${2:-40}
  [[ -n "$pane" ]] || die 1 "usage: worker.sh peek <pane> [lines=40]"
  [[ "$lines" =~ ^[0-9]+$ ]] || die 1 "peek: lines must be an integer"
  tmux has-session -t "$SESSION" 2>/dev/null || die 1 "tmux session '$SESSION' missing"
  window_exists "$pane" || die 1 "peek: no such pane '$pane'"
  tmux capture-pane -p -t "$SESSION:$pane" 2>/dev/null | tail -n "$lines"
}

cmd_list() {
  local info; info=$(collect_panes) || true
  if [[ -z "$info" ]]; then
    printf '(no worker panes)\n'
    return 0
  fi
  local w st idle since size
  while IFS=$'\t' read -r w st idle since size; do
    printf '%s %s idle=%ss size=%s since=%s\n' "$w" "$st" "$idle" "$size" "$since"
  done <<< "$info"
}

# ---- board -------------------------------------------------------------------
cmd_board() {
  local as_json=0 a
  while (( $# )); do
    a=$1; shift
    case "$a" in
      --json) as_json=1 ;;
      *) die 1 "board: unknown option $a (try --json)" ;;
    esac
  done
  local arena_tmp pane_tmp rc
  arena_tmp=$(mktemp); pane_tmp=$(mktemp)
printf '[]\n' > "$arena_tmp"   # (nopper: no platform; board shows graphs/panes only)
  collect_panes > "$pane_tmp" || true
  python3 - "$arena_tmp" "$pane_tmp" "$ROOT" "$MAX_WORKERS" "$as_json" <<'PYBOARD'
import glob, json, os, re, sys, time

arena_path, pane_path, root = sys.argv[1], sys.argv[2], sys.argv[3]
max_workers, as_json = int(sys.argv[4]), sys.argv[5] == "1"

def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default

# -- platform rows (arena list; unreachable platform degrades to empty) --------
plat = {}
for c in (load_json(arena_path, []) or []):
    if isinstance(c, dict) and isinstance(c.get("unique_code"), str):
        plat[c["unique_code"]] = {
            "completed": bool(c.get("is_completed")),
            "flags_correct": int(c.get("correct_flag_count") or 0),
            "flags_total": int(c.get("flag_count") or 0),
        }

# -- graphs (dedupe -wN symlinked graphs by realpath; never trust file contents)
graphs = {}
for pj in sorted(glob.glob(os.path.join(root, "*", ".nopper", "project.json"))):
    d = os.path.dirname(os.path.dirname(pj))
    code = re.sub(r"-w\d+$", "", os.path.basename(d))
    key = os.path.realpath(os.path.join(d, ".nopper"))
    if key in graphs:
        continue
    kinds, latest, total = {}, 0.0, 0
    findings = {"observed": 0, "confirmed": 0, "rejected": 0}
    helps = 0
    for fp in glob.glob(os.path.join(d, ".nopper", "facts", "*.json")):
        total += 1
        try:
            f = json.load(open(fp))
            latest = max(latest, os.path.getmtime(fp))
        except Exception:
            continue
        k = f.get("kind")
        if k:
            kinds[k] = kinds.get(k, 0) + 1
            if k == "glitch_result" and "status" in (f.get("data") or {}):
                # ledger-created findings carry the lifecycle status; plain
                # glitch_result facts from dispatch never do
                st = (f.get("data") or {}).get("status") or "observed"
                findings[st] = findings.get(st, 0) + 1
            if k == "analysis_note" and "[HELP]" in str(f.get("description", "")) + str(f.get("data") or ""):
                helps += 1
    intents = {"open": 0, "claimed": 0, "done": 0, "complete": 0}
    for ip in glob.glob(os.path.join(d, ".nopper", "intents", "*.json")):
        try:
            st = json.load(open(ip)).get("status")
        except Exception:
            continue
        if st in intents:
            intents[st] += 1
    graphs[key] = {"code": code, "facts_total": total, "kinds": kinds,
                   "findings_observed": findings["observed"],
                   "findings_confirmed": findings["confirmed"],
                   "findings_rejected": findings["rejected"],
                   "intents": intents, "latest_mtime": latest, "helps": helps}

by_code = {}
for g in graphs.values():
    cur = by_code.get(g["code"])
    if cur is None or g["facts_total"] > cur["facts_total"]:
        by_code[g["code"]] = g

# -- panes merged in ------------------------------------------------------------
panes, alive = {}, 0
for line in open(pane_path):
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 5:
        continue
    w, st, idle, since, size = parts
    m = re.match(r"^(.*)-w\d+$", w)
    panes.setdefault(m.group(1) if m else w, []).append(
        {"pane": w, "state": st, "idle": int(idle), "since": since, "size": size})
    if st == "alive":
        alive += 1

codes = sorted(set(plat) | set(by_code) | set(panes),
               key=lambda c: (0 if c in panes else (1 if c in by_code else 2), c))
rows = []
for c in codes:
    p, g = plat.get(c) or {}, by_code.get(c) or {}
    rows.append({
        "code": c,
        "completed": p.get("completed"),
        "platform_flags": ([p["flags_correct"], p["flags_total"]] if p else None),
        "facts_total": g.get("facts_total", 0),
        "kinds": g.get("kinds", {}),
        "intents": g.get("intents", {}),
        "findings_observed": g.get("findings_observed", 0),
        "findings_confirmed": g.get("findings_confirmed", 0),
        "findings_rejected": g.get("findings_rejected", 0),
        "helps": g.get("helps", 0),
        "latest": time.strftime("%H:%M", time.localtime(g["latest_mtime"]))
                  if g.get("latest_mtime") else "-",
        "panes": panes.get(c, []),
    })

if as_json:
    print(json.dumps({"now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "workers_alive": alive, "worker_cap": max_workers,
                      "codes": rows}, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0)

# heartbeat to drive.log (=> docker logs): the commander boards every loop,
# so this line IS the campaign pulse for the outside observer.
findings_obs = sum(r["findings_observed"] for r in rows)
findings_conf = sum(r["findings_confirmed"] for r in rows)
findings_rej = sum(r["findings_rejected"] for r in rows)
done_n = sum(1 for r in rows if r["completed"])
# war clock + general census: the commander's dashboard gauges
left = "-"
try:
    dl = float(open(os.path.join(root, ".deadline")).read().strip())
    secs = dl - time.time()
    if secs >= 0:
        left = ("%dh%02dm" % (secs // 3600, (secs % 3600) // 60))
    else:
        left = "OVER"
except Exception:
    pass
generals = 0
try:
    import glob as _g
    for pf in _g.glob(os.path.join(root, "pane-env", "*.env")):
        try:
            m = [l for l in open(pf) if l.startswith("MODEL=")][0][6:].strip()
        except Exception:
            continue
        if m and "flash" not in m and "deepseek" not in m:
            generals += 1
except Exception:
    pass
print(f"[board] heartbeat: codes={len(rows)} done={done_n} "
      f"alive={alive}/{max_workers} findings_observed={findings_obs} "
      f"findings_confirmed={findings_conf} findings_rejected={findings_rej} "
      f"budget_left={left} generals={generals}", file=sys.stderr)

def kinds_str(k):
    if not k:
        return "-"
    order = ["device", "chip", "board", "interface", "firmware_image",
             "probe_point", "vulnerability", "glitch_param", "glitch_result",
             "scan_campaign", "power_profile", "analysis_note",
             "repro_confirmed"]
    # two-letter kind codes (single letters collide: glitch_param/glitch_result,
    # probe_point/power_profile) — e.g. de1 ch2 gp12 gr3
    parts = [f"{n[:2]}{k[n]}" for n in order if k.get(n)]
    parts += [f"{n[:2]}{v}" for n, v in sorted(k.items()) if n not in order]
    return (" ".join(parts) or "-")[:16]

def panes_str(lst):
    if not lst:
        return "-"
    out = []
    for p in lst:
        m = re.match(r"^.*-w(\d+)$", p["pane"])
        lbl = f"w{m.group(1)}" if m else p["pane"]
        out.append(f"{lbl}:{p['idle']}s" if p["state"] == "alive" else f"{lbl}:X")
    return " ".join(out)

widths = [("CODE(⚑=HELP!)", 17), ("DONE", 4), ("FLG", 7), ("FACTS", 6), ("KINDS", 16),
          ("INT o/c", 8), ("FND c/o/r", 9), ("LAST", 5)]
print(f"== nopper board {time.strftime('%H:%M')}  workers alive={alive}/{max_workers}"
      f"  codes={len(rows)} ==")
print(" ".join(t.ljust(w) for t, w in widths) + "PANES")
budget = 100 - 3  # header + column line + trailing guard
for i, r in enumerate(rows):
    if i >= budget:
        print(f"... {len(rows) - i} more codes (use: worker.sh board --json)")
        break
    pf = r["platform_flags"]
    done = "-" if r["completed"] is None else ("yes" if r["completed"] else "no")
    codecell = r["code"][:14]
    if r.get("helps"):
        codecell = "⚑%d %s" % (r["helps"], codecell)[:14 + 3]
    cells = [
        codecell.ljust(17), done.ljust(4),
        (f"{pf[0]}/{pf[1]}" if pf else "-").ljust(7),
        (str(r["facts_total"]) if r["facts_total"] else "-").ljust(6),
        kinds_str(r["kinds"]).ljust(16),
        f"{r['intents'].get('open', 0)}/{r['intents'].get('claimed', 0)}".ljust(8),
        f"{r['findings_confirmed']}/{r['findings_observed']}/{r['findings_rejected']}".ljust(9),
        r["latest"].ljust(5), panes_str(r["panes"]),
    ]
    print(" ".join(cells).rstrip())
PYBOARD
  rc=$?
  rm -f "$arena_tmp" "$pane_tmp"
  return "$rc"
}

# ---- hint --------------------------------------------------------------------
cmd_hint() {
  local code=${1:-} text=${2:-}
  [[ -n "$code" && -n "$text" ]] || die 1 'usage: worker.sh hint <code> "<text>"'
  is_code "$code" || die 1 "hint: invalid code '$code'"
  write_hint "$code" "$text"
}

usage() {
  cat >&2 <<'USAGE'
worker.sh — the commander's hands (one-shot subcommands; no main loop)
  spawn <code> [--tui|--p] [--mission "..."] [--minutes N] [--name NAME] [--model <id>]
                                     [--gw glm|ds] [--engine claude|codex] [--general]
                                     --model: worker brain (default flash tier; smart =
                                     'glm-5.3[1m]' — SKILL 4.9 tiering rules)
                                     --general: smart-tier GENERAL that may hire its own
                                     -p squads on the same challenge (SKILL 4.10)
  kill  <pane>|<code> [--close]      --close closes the instance (findings stay as
                                     recorded facts) only when no sibling pane of
                                     that code lives
  msg   <pane> "<text>"              send-keys -l + Enter (never bracketed paste)
  peek  <pane> [lines=40]
  list                               <pane> alive|exited idle=<s> size=WxH since=<hh:mm>
  board [--json]                     the war board (<100 lines)
  hint  <code> "<text>"              append a [commander] hint fact to that graph
exit codes: 0 ok | 1 generic error | 3 platform cap (max active) | 4 worker cap
USAGE
}

case "${1:-}" in
  spawn) shift; cmd_spawn "$@" ;;
  kill) shift; cmd_kill "$@" ;;
  msg) shift; cmd_msg "$@" ;;
  peek) shift; cmd_peek "$@" ;;
  list) shift; cmd_list "$@" ;;
  board) shift; cmd_board "$@" ;;
  hint) shift; cmd_hint "$@" ;;
  help|-h|--help) usage; exit 0 ;;
  "") usage; exit 1 ;;
  *) printf '[worker] ERROR: unknown subcommand %s\n' "$1" >&2; usage; exit 1 ;;
esac
