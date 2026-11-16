# nopper — orchestration framework for hardware security research

<p align="center"><img src="docs/assets/banner.jpg" alt="nopper — hardware security research orchestration" width="860"></p>

**nopper** is a multi-agent orchestration harness for hardware security
research: chip fault injection, firmware analysis, instrument-driven
characterization. It is not a single agent — it is a small fleet: one AI
commander reads the board, assigns work, and verifies results; workers do
interface characterization, firmware extraction, glitching, and reproduction;
a zero-policy watchdog keeps everything alive across crashes and context resets.

All shared state lives in one **fact graph**: a DAG of typed facts (devices,
chips, interfaces, firmware images, glitch parameters/results, scan campaigns,
power profiles) and intents (units of experiment work), one JSON file per
node, guarded by file locks — no database, no server, pure Python stdlib.

> **Provenance.** nopper is a fork of a multi-agent pentest-competition
> framework (sapper-v2, champion of an autonomous attack-defense challenge).
> The competition layer — the scoring platform bridge, the bench driver, the
> flag submission pipeline — has been removed; the generic orchestration core
> (fact graph, dispatch engine, fleet/watchdog, knowledge base) is unchanged
> and is what this fork builds on. Historical records of the sapper era are
> kept in `CHANGELOG.md` and `docs/`.

## Mechanism, not model

nopper does not bind to any AI model or CLI — every role's agent and model is
replaceable:

| Lever | How to change it |
|---|---|
| **Agent** | any Claude-Code-compatible CLI (claude / codex / custom `NOPPER_BIN`) |
| **Model** | `--model` flag or `NOPPER_MODEL` / `NOPPER_MODEL_FLASH` env |
| **Gateway & keys** | `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `NOPPER_KEY_POOL` (watchdog auto-rotates on quota errors) |
| **Budgets** | `NOPPER_TOTAL_MINUTES`, per-step `--minutes`, `--deadline` |
| **Doctrine** | `docker/commander/SKILL.md` + `templates/*.md` (plain markdown) |
| **Fact kinds** | 13 domain kinds in `lib/nopper.py` (device/chip/board/interface/firmware_image/probe_point/vulnerability/glitch_param/glitch_result/scan_campaign/power_profile/analysis_note/repro_confirmed) |

nopper's value is not in any single agent, but in the **mechanism** that makes
many agents cooperate: dispatch, division of labor, communication, memory,
self-healing — swap every agent and model, and the mechanism keeps running.

## Quick start (WSL2 / Linux)

```bash
bash install.sh                       # symlink the nopper CLI (stdlib-only, zero deps)
cd your-experiment-project
nopper setup --goal "characterize target board, find glitch window" --agents
nopper fact "SWD interface confirmed on PA13/PA14" -k interface -d chip=stm32f103 -d evidence_level=observation -d confidence=high
nopper finding "ext_offset=47 width=32 skip" -c c01    # finding ledger (dedupe, crash-safe)
nopper dispatch explore i001          # one executor step
nopper auto --minutes 60              # reason -> explore -> verify loop to completion
nopper validate                       # audit the graph (+ advisory evidence warnings)
nopper view                           # self-contained HTML DAG
```

For the tmux fleet (commander + workers + watchdog), see
`docker/commander/SPEC.md` and `docker/commander/SKILL.md`; the fleet runs
natively in WSL2 (tmux + fcntl required).

## Findings, not flags

The ledger records **observed experimental results** — dedupe on
(campaign, value), record-first crash-safety, and a three-state lifecycle:
`observed` -> `confirmed` (reproduction statistics pass) or `rejected`.
Knowledge accumulates in the offline kb (`nopper kb search "voltage glitch"`).

## Safety & scope

Hardware experiments are physical: read `DISCLAIMER.md`, keep work on
equipment you own or are authorized to test, and treat the doctrine's safety
envelope (voltage limits, reset interlocks) as hard rules, not advice.

## License & upstream

MIT. Upstream generic base: [nvwabase](https://github.com/N1nEmAn/nvwabase)
(agents / language / history capabilities). See `LICENSE` and the upstream
attribution in the repository history.
