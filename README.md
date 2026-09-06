# kill-dog — RunPod GPU fleet billing guard

**One-line:** a kill-dog that terminates any GPU pod this account leaves
running past its TTL or (local layer only) demonstrably idle, so a dead
agent session can never leave an instance billing for hours.

**Origin incident (bench r3, 2026-09-04):** the sandbox tool service died
mid-flight; the session was dead ~2h while the L40 kept billing (~$1.40).
The only idle-kill logic that existed (comfy-backend autoscaler,
`scale_down_idle_seconds=600`) runs *inside the backend app* — when the
driving session dies, nothing is watching. This repo now ships its own
guard, independent of the backend client code.

## Architecture

```
pod created (env: KILLDOG_TTL_MIN=360)
   │
   ├── L1 LOCAL DFORK DAEMON (first line, fast)
   │     spawn_kill_dog.py <pod_id>     # double-fork → PID 1
   │     survives: toolcall ends, tool-service EOF, session exit
   │     dies only on: sandbox recycle
   │     enforces: hard TTL + IDLE (heartbeat file + /queue + SSH check)
   │
   ├── L2 GHA SAFETY NET (second line, slow, survives sandbox recycle)
   │     .github/workflows/kill-dog.yml on the DEFAULT branch
   │     cron */20 + workflow_dispatch; public repo → free minutes
   │     RUNPOD_API_KEY from repo secrets (never in the tree)
   │     enforces: hard TTL only — can never false-kill a live session
   │
   └── L3 ONE-SHOT AUDIT (session start/end / drills)
         python3 ops/killdog/kill_dog.py --once [--dry-run] [--json]
```

**Division of labor:** L1 is fast (60s poll) and context-aware (it can see
the driver's heartbeat), L2 is slow (20 min) but lives on GitHub infra and
survives the recycle that kills L1. TTL breach → either layer terminates
(terminate is idempotent; races are harmless).

## Kill rules

| Rule | Enforced by | Condition |
|---|---|---|
| TTL breach | L1 + L2 | `age_min > ttl_min`, age = `runtime.uptimeInSeconds`, ttl = pod env `KILLDOG_TTL_MIN` else default (L2: 240) |
| Idle | L1 only | driver heartbeat file not touched `--idle-min` (30) AND ComfyUI `/queue` reachable+empty AND no wget/curl via SSH (if key given), confirmed `--idle-confirmations` (3) consecutive polls |

Unknown probes (queue unreachable, SSH auth fail) never count as idle and
never reset the counter. Non-running pods cost nothing → reported only.

**Kill = TERMINATE, never stop.** RunPod community-pod stop wipes the
container disk (r3 lesson: 94 GB re-download) and only terminate reliably
ends the meter.

## Setup (one-time, per credential rotation)

```
python3 ops/killdog/ensure_gha_net.py --install --set-secret
# --install:      syncs workflow + kill_dog.py onto the default branch
# --set-secret:   RUNPOD_API_KEY via PyNaCl sealed box (needs PAT)
```

PAT comes from `--token` / `GITHUB_TOKEN` / `/home/z/bench-r3/.gh_pat`;
RunPod key from the bench repo `.env` (see `ensure_gha_net.py`).
**After rotating the RunPod key, re-run `--set-secret`.**

## Drill (validated 2026-09-06, ~$0.2 total)

1. GHA net: create pod with `KILLDOG_TTL_MIN=6`, no local dog, dispatch
   the workflow after 7 min → run log shows TTL breach → TERMINATED.
2. Local dog: create pod, spawn dog with `--idle-min 2` and a heartbeat
   file that is never touched again → queue drains → idle-confirmed ×3 →
   TERMINATED.

## Live API facts baked into this code (2026-09-06)

- GraphQL `myself { pods { … env runtime { uptimeInSeconds ports { ip
  isIpPublic privatePort publicPort } } } }` is the working read path;
  **`portType` is NOT a valid field on the pods-list selection** (400
  GRAPHQL_VALIDATION_FAILED) — match ports by `privatePort` instead.
- `mutation TerminatePod($input: PodTerminateInput!) { podTerminate(input:
  $input) }` — returns Void, no selection set.
- `env` in the selection returns the pod's env; our create path bakes
  `KILLDOG_TTL_MIN`.
- urllib's default UA is Cloudflare-banned (error 1010) — always send
  `python-requests/2.31.0`.
- ComfyUI queue probe: `https://<podId>-<privatePort>.proxy.runpod.net/queue`
  → `{"queue_running": [...], "queue_pending": [...]}`.
