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
   │     ACTIVE host: beulahkemp/kill-dog (public repo, working runners)
   │       cron */20 + workflow_dispatch, RUNPOD_API_KEY repo secret
   │     DORMANT hosts (installed; come alive when the trinitylivy
   │       account's Actions billing lock is cleared — run annotation:
   │       "The job was not started because your account is locked due
   │       to a billing issue", every workflow since 2026-09-04):
   │       trinitylivy/kill-dog + trinitylivy/comfy-templates@main
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

## Drills (validated 2026-09-06, ~$0.22 total)

**Drill A — GHA net TTL-kill** (pod `rs63osd5rdhv0t`, L40S SECURE, env
`KILLDOG_TTL_MIN=6`, no local dog = "local layer died"):
- young pod correctly spared: `age=2.4 ttl=6 … ok`
- breach killed: `KILL rs63osd5rdhv0t: TTL breach 6.9m > 6m -> TERMINATED`
- validates: env-at-create (REST v2 `"env"`), GraphQL env read-back,
  dispatch → terminate chain, secret plumbing.

**Drill B — local dfork idle-kill** (pod `h00cvk5enw2ofr`, L40S SECURE,
env TTL=30 as backstop, heartbeat touched once then never = "driver died"):
```
10:26  ok      age=1.9 ttl=30 hb=2.0 queue=empty   (confirmations start)
10:27  ok      age=2.9 ttl=30 hb=3.1 queue=empty
10:28  KILL h00cvk5enw2ofr: IDLE -> TERMINATED (heartbeat stale 4.1m,
      queue empty, downloads=unknown, confirmed 3x)   [TERMINATED]
```
- validates: double-fork survival across bash toolcalls (dog PID 2432,
  PPID 1, outlived 3+ toolcalls), heartbeat staleness detection, queue
  probe, 3-confirmation guard, terminate path.
- vs the r3 incident: 2h idle billing → **4.2 min**. Sandbox-recycled case
  (local dog dead) is bounded by the net's TTL instead (6h default).

Note: on SECURE-cloud pods there is no public SSH port — the download
probe reports `unknown` and the idle rule relies on heartbeat+queue alone
(unknown never blocks a kill here because the probe is advisory; it only
prevents kills when it sees active downloads).

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
