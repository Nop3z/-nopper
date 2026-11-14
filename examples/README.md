# nopper — worked examples

## Example 1 — a debugging campaign (generic fact-graph loop)

The goal is to find why `python reproduce.py` segfaults.

```bash
# 0. set up a workspace
mkdir -p /tmp/nopper-demo && cd /tmp/nopper-demo
cat > reproduce.py <<'EOF'
import sys
print("loading module X")
# ... imagine this segfaults deep in a C extension
raise SystemExit("segfault would happen here in the real case")
EOF

# 1. create the graph
nopper init --origin "user report: segfault" --goal "find root cause of segfault in reproduce.py"

# 2. see it
nopper status
nopper graph                    # full graph as text

# 3. commander proposes the first intents (DRY RUN first to see the prompt)
nopper dispatch reason --dry-run

# 4. for real — the model reads the graph and writes intents into .nopper/
nopper dispatch reason

# 5. work the first proposed intent
nopper pick                       # -> i001
nopper dispatch i001

# 6. verify what it claimed
nopper dispatch verify --intent i001

# 7. loop: reason -> explore -> verify until the commander returns {"complete":...}
nopper dispatch reason

# 8. finish
nopper complete --from f003 --note "C extension double-free, patched in v2.1"

nopper status                     # status: completed
```

## Example 2 — a hardware characterization experiment (nopper domain)

```bash
mkdir -p runs/c01 && cd runs/c01
nopper init --goal "experiment c01: characterize target board, locate glitch window"

# rig characterization lands as typed facts (evidence metadata encouraged)
nopper fact "target board rev C, 3V3 rail" -t "目标板确认" -k device -d name=target-c01 -d fw_version=1.2 -d evidence_level=observation -d confidence=high
nopper fact "STM32F103 part, 72 MHz core" -k chip -d part_no=STM32F103C8 -d core=cortex-m3 -d freq_mhz=72
nopper fact "SWD on PA13/PA14, no readout protection" -k interface -d kind=swd -d chip=STM32F103C8 -d pins=PA13,PA14 -d protected=false
nopper fact "bootloader jump point is the injection anchor" -k probe_point -d signal=BOOT0 -d trigger_desc="falling edge after ROM copy"

# a successful glitch result: record it in the finding ledger immediately (crash-safe)
nopper fact "ext_offset=47 width=32 skipped the jump check" -k glitch_param -d mode=voltage -d v_target_mv=2700 -d width_ns=32 -d ext_offset=47
nopper finding "ext_offset=47 width=32 skip" -c c01
nopper status
#   kinds:  1 devices, 1 chips, 1 interfaces, 1 glitch_params ...
#   finding: f006 status=observed challenge=c01 value=ext_offset=47 width=32 skip

# an autonomous run bounded by lab time
nopper auto --skip-permissions --deadline "2026-11-20T13:00:00"

# several target boards, round-robin
cd .. && nopper sweep --root runs --minutes-per 20 --deadline "2026-11-20T13:00:00" --skip-permissions
```

## resuming

The graph is on disk. Close the terminal, come back tomorrow:

```bash
cd runs/c01
nopper status      # picks up exactly where you left off
nopper graph       # full history of facts + intents
```
