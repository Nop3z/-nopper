# Commander Architecture SPEC — v2 (2026-11-15, nopper doctrine sync)

> Contract for the three implementation units (worker.sh / watchdog.sh / SKILL.md).
> v1 (2026-10-16) described the competition-platform era; v2 re-aims the same three
> units at the nopper hardware-research campaign. Mechanisms are UNCHANGED — only
> semantics, doctrine vocabulary, and the anchor table below are authoritative now.
> Reference implementation for self-heal primitives: `docker/tmux-drive.sh` (read it first).
> All units live in `docker/commander/`, shipped to the image at `/opt/nopper/docker/commander/`.

## Runtime layout

~~~
tmux session "nopper-cmd"
  window 0 "commander"   — claude TUI, the AI commander loop
  window N "<pane-name>" — worker panes (TUI or -p), spawned/killed by commander
  watchdog               — the dumb guardian script (runs detached via nohup)
ROOT=/work/runs (NOPPER_ROOT)
  <code>/               — campaign home; .nopper/ fact graph lives here
  <code>-w2/            — second agent on same campaign: .nopper -> ../<code>/.nopper SYMLINK
  commander-state.md    — commander's self-maintained campaign state (handover doc)
  drive.log             — watchdog + worker log (tail -F to stdout by entrypoint)
  .deadline             — epoch the watchdog writes; board heartbeat reads it
  pane-env/<pane>.env   — per-pane MODEL/ENGINE/MISSION_B64 (watchdog relaunch contract)
~~~

## Environment

`ANTHROPIC_BASE_URL/AUTH_TOKEN` (single gateway), `NOPPER_KEY_POOL` (JSON array),
`NOPPER_TOTAL_MINUTES` (default 470 — the campaign war clock),
`NOPPER_MAX_WORKERS` (default 8 — hard pane cap).

**Model tiering (SKILL §4.9, researcher decision 2026-11-15: GLM thinks, DeepSeek works):**
- `NOPPER_MODEL` (default `glm-5.3`) — the SMART tier: the commander's own TUI and
  GENERAL workers the commander assigns to hard chains (`spawn --general`).
- `NOPPER_MODEL_FLASH` (default `deepseek-v4.1-flash`) — the LINE tier. Default
  model for spawned workers: scouts, glitch operators, verifiers, -p chores.
- `worker.sh spawn --model <id>` overrides per spawn; default = `$NOPPER_MODEL_FLASH`.
- worker.sh persists each pane's model to `ROOT/pane-env/<pane>.env` (KEY=VAL,
  sourceable) so watchdog relaunches keep the same tier. Watchdog fallback:
  `$NOPPER_MODEL`. Key-pool rotation may change provider: rotated.env wins on the
  relaunch after rotation (see watchdog rotate_key).

**No competition platform exists.** There is no instance cap, no scoring server,
no arena bridge. The scarce resources are RIGS (benches) and device lifetime;
doctrine default: ONE operator pane per rig (SKILL §0).

## D3 legacy markers — platform-era five-piece leftovers (harmless, kept)

The platform bridge is gone but these output-shape compatibility stubs remain;
do not build on them, do not "fix" them without reading worker.sh headers:
1. `finish_flags()` in worker.sh AND watchdog.sh (two verbatim stubs) — logs
   "no submission target; findings stay as recorded facts". Call sites kept.
2. `close_instance()` in worker.sh — no-op stub (arena removed); `kill --close`
   reduces to findings close-out + `.addrs` cleanup.
3. `.addrs` file — no longer written by spawn; kept in cleanup paths for
   output compatibility only.
4. board's platform rows (`plat` in PYBOARD) + DONE/FLG columns — always
   empty; the live columns are FACTS/KINDS/INT/FND/PANES.
5. spawn exit code 3 (platform cap) — RETIRED, never produced. The usage text
   still mentions it; cleanup candidate for a later mechanical commit. SKILL
   §3.1 documents 0 / 4 / generic as the live codes.

## Unit 1: worker.sh — the commander's hands (called BY the commander)

`#!/usr/bin/env bash`, `set -o pipefail`. Subcommands (exit non-zero + stderr message on failure).
Must be idempotent and safe to call repeatedly. No main loop — one-shot commands.

### `spawn <code> [--tui|--p] [--mission "..."] [--minutes N] [--name NAME] [--model <id>] [--general] [--engine claude|codex]`
1. No instance acquisition (no platform). Rig binding is DOCTRINE (SKILL §0/§4.2),
   not code: the commander reads STATE's rig allocation before spawning.
2. Prepare dirs: if primary agent (`-w1`): `mkdir -p ROOT/<code>`, guarded
   `nopper init --goal "experiment <code>: ..."`. If secondary (`-w2+`):
   `mkdir -p ROOT/<code>-wN`, `ln -sfn ../<code>/.nopper ROOT/<code>-wN/.nopper`.
3. Trust preset (per-project hasTrustDialogAccepted + bypassPermissions settings).
4. TUI mode: `tmux new-window ... "cd ROOT/<code>[-wN] && claude --model $MODEL --effort max --dangerously-skip-permissions 2>&1; echo DRIVE_PANE_EXITED; exec sleep infinity"`
   (NO --continue: fresh session per pane; recovery uses cwd-scoped --continue by watchdog —
   different cwd ⇒ different session dir ⇒ no cross-talk between w1/w2).
5. `-p` mode: window command = `nopper auto --store ROOT/<code>/.nopper --minutes <N|10> ... ; echo DRIVE_PANE_EXITED; exec sleep infinity`.
6. Wait for input box (≤75s, auto-accepting dialogs), then `send_literal` the mission.
   Default missions: `default_mission()` opens with the ANCHORED phrase (see anchor
   table) and assigns the nopper operator role (facts, finding ledger, [HELP]/[REPORT]
   via analysis_note). `general_mission()` = smart-tier analyst allowed to hire -p
   squads. --mission overrides. Retry mission once if not confirmed in pane.
7. Print `SPAWNED <pane> addrs= model=<id>` on success (addrs vestigial, always empty).

### `kill <pane> | <code> [--close]`
Kill window(s). `--close` runs the findings close-out stub and clears the rig
binding marker (NOTE: only when NO other pane of that code remains alive — the
guard keeps sibling findings untouched).

### `msg <pane> "<text>"` — send-keys -l + Enter (bracketed paste is unreliable — NEVER use load-buffer/paste-buffer).

### `peek <pane> [lines=40]` — capture-pane tail (commander watches worker screens).

### `list` — one line per pane: `<pane> alive|exited idle=<s> size=<cols>x<rows> since=<hh:mm>`.

### `board` — the war board. Aggregate per campaign (graphs deduped by .nopper
realpath so -wN symlinks count once): facts by kind (13 hardware kinds,
two-letter codes), open intents o/c, finding ledger confirmed/observed/rejected,
open [HELP] notes (⚑ in CODE column), latest fact mtime, panes merged in;
stderr heartbeat line = campaign pulse (findings counts, budget_left, generals).
Compact aligned text (<100 lines; gets pasted into the commander TUI) + `--json`.

### `hint <code> "<text>"` — append a hint fact to that campaign's graph (via
`nopper hint`; content prefixed `[commander]`).

### Hard cap: refuse spawn when live worker panes >= NOPPER_MAX_WORKERS (exit 4, `WORKER CAP`).

Exit codes: 0 ok | 1 generic | 4 worker cap. (3 = platform cap: retired.)

## Unit 2: watchdog.sh — the guardian (dumb loop, NO decisions)

`set -o pipefail`. Single loop, sleep 15s. State in declare -A maps. Logs `[watch] ...` to drive.log.

Responsibilities IN ORDER per sweep:
1. **Commander pane dead** (DRIVE_PANE_EXITED or window gone) → relaunch claude in window
   "commander" with `--continue` (commander has its OWN cwd ROOT/commander-home so --continue
   is safe), wait for input box, inject bootstrap prompt:
   `Read $SKILL_PATH and resume command. First read ROOT/commander-state.md if it exists.`
   (first boot: same but "start commanding").
2. **Unsubmitted bootstrap text in commander pane** (input box `❯` + SKILL.md path present,
   no activity) → re-Enter (3 tries, then restart commander window).
2b. **Commander hang failsafe**: pane content unchanged 30 min AND empty input box → relaunch.
3. **Confirm dialogs in ANY pane** → Down+Enter (accept_dialogs, aimed by the ❯ focus marker).
4. **Quota/auth errors in ANY pane** (QUOTA_RE) → rotate_key(); on success restart that pane:
   TUI workers relaunch with cwd-scoped `--continue`; commander relaunch same as (1);
   -p workers: just log (commander will notice via board). Transient rate limits
   (RATELIMIT_RE) rotate NOTHING. Rotation never burns restart budget.
4b. **TUI worker mission stuck unsubmitted** → re-Enter x3, then restart window, then
   leave it for the commander. Detection = ANCHOR regex (see table) against worker.sh
   default_mission's opening phrase — the two files must move TOGETHER.
5. **TUI worker DRIVE_PANE_EXITED** → auto-relaunch with `--continue` (same cwd), max 3/pane
   (restarts map), budget-independent. -p workers exited = normal completion: leave the pane
   (commander reads result via peek, kills via worker.sh kill).
6. **Global deadline** (NOPPER_TOTAL_MINUTES from script start) → C-c all worker panes,
   finish_flags for EVERY code with a graph (stub), log summary, exit 0. Failsafe finish
   when the commander is dead at the buzzer.
7. Worker TUI stall (no pane change > NOPPER_STALL_SECONDS=600) → do NOTHING (nudging is the
   commander's job; watchdog only keeps processes alive).

ALSO: watchdog must NOT do campaign selection, spawning, killing, mission text — zero strategy.
Startup: `mkdir -p ROOT`, log env summary, ensure tmux session exists, adopt-existing state,
write `ROOT/.deadline` (single source of truth for budget_left).

### ANCHOR TABLE — literal strings that MUST stay in lockstep (arch-analysis §4.2)

| anchor | file:line | locked against | rule |
|---|---|---|---|
| `❯.*autonomous experiment operator` | watchdog.sh mission-stuck check (ANCHOR comment above it) | worker.sh `default_mission` opening phrase "You are an autonomous experiment operator ..." | change BOTH together or mission-stuck detection silently dies |
| `❯.*docker/commander/SKILL\.md` | watchdog.sh commander bootstrap check | entrypoint.sh + watchdog relaunch_commander bootstrap text | keep the SKILL path in the bootstrap text |
| `DRIVE_PANE_EXITED; exec sleep infinity` | worker.sh / watchdog.sh / entrypoint.sh window commands | pane death detection (list/board/watchdog) | NEVER drop from any window command |
| `Enter to confirm` / `❯.*No, exit` | accept_dialogs in worker.sh + watchdog.sh | claude-code trust dialogs | keep both aim + confirm steps |
| QUOTA_RE / RATELIMIT_RE | watchdog.sh | gateway error strings | update only with live gateway evidence |

## Unit 3: SKILL.md — the commander's brain (nopper doctrine, v2)

Frontmatter-free markdown, injected path /opt/nopper/docker/commander/SKILL.md. Section skeleton:

0. **Fixed facts**: worker/ROOT/STATE paths; NO platform (no instance cap, exit 3
   retired); RIG INVENTORY & bench occupancy (1 operator pane per rig); THREE hard
   budgets — shot budget / energy budget / device-lifetime budget; model tiers; key
   pool; shared graph per campaign; /mnt/d exchange convention (WSL2 orchestrates,
   Windows-side hwctl acts).
1. **Role**: field commander of a hardware-experiment campaign. NEVER touches
   hardware, never shoots, never reverse-engineers. Allowed actions EXACTLY:
   worker.sh + read-only nopper (status/graph/kb search/scan status) + pane output
   + STATE + sleep.
2. **The loop** (run forever until deadline): read STATE (fresh start) → board →
   think (HELP answer / observed-finding needs verify / stuck worker / cross-
   campaign transfer / free rig+pane) → ≤2 decisive actions → sleep 60 → STATE
   checkpoint.
3. **Tool manual**: worker.sh subcommands + exit codes; nopper read-only face;
   hwctl face (PLACEHOLDER — enabled after W2); fi face (discover/sim/schema,
   RISC-V campaigns); finding-ledger semantics (observed→confirmed/rejected).
4. **Doctrine**: 4.1 low-risk-first sweep (recon/analysis ALWAYS before injection);
   4.6 escalation table + INTERLOCK stop-ladder (3 all-crash batches → rail to
   nominal + human reset confirmation before ANY resume; rig loss >60s → freeze;
   crash budget cap; success stops the scan); 4.7 finding-ledger policing; 4.8
   scope hard lines (NEVER out-of-envelope / unledgered shots / unverified success;
   firmware extraction researcher-only, agents register sha256; physical action =
   hwctl only); 4.9 model tiering (kept verbatim); 4.10 recon-first classification
   + HARD-VERDICT (dispatch-verify-repro criteria: Poisson zero-count ≤1e-3, 3σ
   MAD rise, baseline three-way, drift guard).
5. **Self-management**: 85/90 compaction protocol, STATE template (rig allocation,
   interlock status, shot/crash counts, method log, queue), <150 lines.
6. **Failure awareness**: watchdog behaviors + interlock handling + halftime audit
   + endgame (bank confirmations, release rigs).
7. **Strategic craft**: 7.0 family migration via kb gate (applicability ∧
   verified:true for AUTOMATIC window prefill; unverified = advisory; event-
   anchored relative offsets + rig context only; confidence ≤0.6 until locally
   confirmed) + the craft habits (survey first, protect the DUT, extend around
   confirmed windows, kb is free).
8. **NEVER** list.

Templates set (engine side, `templates/`): reason (commander proposal, low-risk-
first + success-stops-scan), capture (single-point experiment, four-question
gate, glitch_param/glitch_result contract; replaces explore.md), verify_repro
(replay statistics + FI live_plan criteria; replaces verify.md), command
(campaign allocation), blindspot (experiment-design blindspots), brainstorm
(multi-injection-route divergence), scan (pre-scan review; hyperparameter
whitelist only — enabled with dispatch-scan in W2).

## entrypoint.sh (unit 4, trivial)
Env check (ANTHROPIC_* mandatory; platform preflight removed) → gateway smoke test
→ tmux new-session -d -s nopper-cmd -n commander → `nohup watchdog.sh &`
→ spawn commander claude TUI in window 0 (cwd ROOT/commander-home) with bootstrap prompt.
Container CMD stays foreground on the watchdog (tmux attach alternative: `tail -f drive.log`).
The bootstrap text is an ANCHOR input (watchdog check 2 greps it in the pane).

## Dockerfile.commander
FROM nopper-agent:tmux (inherits everything), COPY docker/commander/* to /opt/nopper/docker/commander/,
chmod +x worker.sh watchdog.sh entrypoint.sh, ENTRYPOINT entrypoint.sh.
(D3 disposition: WSL2 native tmux fleet is the primary run mode; the container is
the server-deployment option only.)

## Known traps (inherited — DO NOT regress)
- bracketed paste breaks ink TUIs → send-keys -l only
- first-boot trust dialogs → preset hasTrustDialogAccepted + Down+Enter auto-accept
- same-cwd --continue cross-talk → per-agent cwd with .nopper symlink
- shared-platform contention → gone with the arena; replaced by RIG contention
  (doctrine problem: one operator pane per rig, SKILL §0/§4.2)
- bash assoc arrays for string-keyed maps → declare -A
- worker panes must end with `echo DRIVE_PANE_EXITED; exec sleep infinity` so death is detectable
- pane-env MODEL values are RAW, never bash %q (the `[1m]` suffix breaks escaped)
- TUI missions are archived base64 in pane-env (MISSION_B64) so watchdog relaunches can re-brief

Tone: imperative, checklist-heavy, no fluff. The commander model is glm-5.3 — keep
instructions literal and unambiguous, avoid judgment calls requiring nuance.