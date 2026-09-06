#!/usr/bin/env python3
"""kill-dog — fleet kill-dog for RunPod GPU instances (the billing guard).

WHY THIS EXISTS
  During bench round 3 the sandbox tool service died mid-flight; the session
  was dead for ~2h while the L40 pod kept billing ($1.40 wasted). Nothing
  reaped it because the only idle-kill logic that exists (comfy-backend
  autoscaler: scale_down_idle_seconds=600) runs *inside the backend app* —
  when the driving session dies, nothing is watching.

LAYERS (this file is the shared engine for all of them)
  1. LOCAL DFORK DAEMON (first line): `spawn_kill_dog.py` double-forks this
     script into PID 1. It survives bash-toolcall ends, tool-service EOFs,
     and session exit (dies only on sandbox recycle). Fast 60s poll, enforces
     BOTH hard TTL and idle rules, reads the driver's heartbeat file.
  2. GHA SAFETY NET (second line): `.github/workflows/kill-dog.yml` on the
     repo's default branch runs this script every 20 min (public repo = free
     minutes) with RUNPOD_API_KEY from repo secrets. It survives sandbox
     recycles — the case that kills layer 1. Enforces hard TTL only
     (conservative defaults so it cannot false-kill a live session).
  3. ONE-SHOT AUDIT: `kill_dog.py --once [--dry-run]` at session start/end.

DESIGN RULES
  - SELF-CONTAINED on purpose: duplicates ~80 lines of RunPod API calls
    instead of importing comfy-backend's client, so an API-drift bug in the
    backend client cannot also kill the guard (independent failure modes).
    Stdlib only — the GHA runner needs zero installs.
  - Kill = TERMINATE (never stop: community-pod stop wipes container disk,
    and terminate is the only call that reliably ends the meter).
  - Idempotent: both layers may race to terminate — podTerminate is
    idempotent, races are harmless.
  - TTL is bounded worst-case cost: default 240 min for untagged pods;
    our create path bakes KILLDOG_TTL_MIN (default 360) into pod env.
  - Account-wide scope: every pod under this RunPod key is ours (bench
    account). If the backend autoscaler ever runs long-lived workers with
    this key, bake a larger KILLDOG_TTL_MIN in their create path.

KILL RULES (per pod, running pods only)
  TTL  : age_min > ttl_min   -> terminate   [both layers]
         age = runtime.uptimeInSeconds; ttl = pod env KILLDOG_TTL_MIN,
         else --default-ttl-min (GHA passes 240).
  IDLE : (daemon only, needs --heartbeat-file) heartbeat file not touched
         for --idle-min minutes AND ComfyUI queue reachable+empty AND
         (SSH check ok with no wget/curl downloads, if a key is supplied)
         observed --idle-confirmations consecutive polls -> terminate.
         Unknown probes (queue unreachable, SSH auth fail) do NOT count and
         do NOT reset the counter. GHA never uses this rule.

USAGE
  kill_dog.py --once [--dry-run] [--json] [--default-ttl-min 240]
  kill_dog.py --daemon --heartbeat-file /home/z/killdog/hb-<pod>.stamp \
      [--idle-min 30] [--idle-confirmations 3] [--poll-secs 60]
  kill_dog.py --once --default-ttl-min 1   # force-kill drill
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

GQL_URL = "https://api.runpod.io/graphql"
UA = "python-requests/2.31.0"  # urllib default UA is Cloudflare-banned (err 1010)

LIST_QUERY = """
query { myself { pods {
  id name desiredStatus costPerHr gpuCount imageName lastStartedAt env
  runtime { uptimeInSeconds ports { ip isIpPublic privatePort publicPort } }
} } }
"""

TERMINATE_MUTATION = """
mutation TerminatePod($input: PodTerminateInput!) { podTerminate(input: $input) }
"""

STATE_DIR = "/home/z/killdog"


def get_key(cli_key):
    if cli_key:
        return cli_key
    if os.environ.get("RUNPOD_API_KEY"):
        return os.environ["RUNPOD_API_KEY"]
    for path in (f"{STATE_DIR}/.rkey", "/home/z/bench-r3/.rkey"):
        if os.path.exists(path):
            v = open(path).read().strip()
            if v:
                return v
    env = "/home/z/comfyui_backend/mvp-app/.env"
    if os.path.exists(env):
        for line in open(env):
            if line.startswith("RUNPOD_API_KEY=rpa_"):
                return line.strip().split("=", 1)[1]
    raise SystemExit("kill-dog: no RunPod API key (use --key / RUNPOD_API_KEY / .rkey file)")


class RunPod:
    def __init__(self, key):
        self.key = key

    def _post(self, payload):
        req = urllib.request.Request(
            f"{GQL_URL}?api_key={self.key}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"RunPod HTTP {e.code}: {body}") from e

    def list_pods(self):
        resp = self._post({"query": LIST_QUERY})
        if resp.get("errors"):
            raise RuntimeError(f"list_pods GraphQL error: {json.dumps(resp['errors'])[:300]}")
        return (resp.get("data") or {}).get("myself", {}).get("pods", []) or []

    def terminate(self, pod_id):
        resp = self._post({"query": TERMINATE_MUTATION, "variables": {"input": {"podId": pod_id}}})
        if resp.get("errors"):
            raise RuntimeError(f"terminate GraphQL error: {json.dumps(resp['errors'])[:300]}")
        return True


def pod_env_dict(raw):
    """GraphQL env read-back shapes seen live: a list of 'K=V' STRINGS
    (2026-09-06 drill), possibly a list of {key,value} dicts, or a plain
    dict — normalize all of them."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    out = {}
    if isinstance(raw, list):
        for kv in raw:
            if isinstance(kv, dict) and "key" in kv:
                out[kv["key"]] = kv.get("value")
            elif isinstance(kv, str) and "=" in kv:
                k, v = kv.split("=", 1)
                out[k] = v
    return out


def resolve_ttl_min(pod, default_ttl_min):
    env = pod_env_dict(pod.get("env"))
    try:
        v = int(env.get("KILLDOG_TTL_MIN", ""))
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return default_ttl_min


def queue_probe(pod):
    """Return 'empty' | 'busy' | 'unknown' by probing ComfyUI /queue via the
    RunPod proxy URL (https://<podId>-<privatePort>.proxy.runpod.net)."""
    ports = ((pod.get("runtime") or {}).get("ports") or [])
    http_port = None
    for p in ports:
        if p.get("privatePort") in (8188, 3000):
            http_port = p.get("privatePort")
            break
    if not http_port:
        return "unknown"
    url = f"https://{pod['id']}-{http_port}.proxy.runpod.net/queue"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        running = len(d.get("queue_running") or [])
        pending = len(d.get("queue_pending") or [])
        return "busy" if (running or pending) else "empty"
    except Exception:
        return "unknown"


def ssh_download_probe(pod, key_path):
    """Return 'downloading' | 'idle' | 'unknown' — count wget/curl procs via
    the pod's public SSH port. Any failure => unknown (never kills on unknown)."""
    ports = ((pod.get("runtime") or {}).get("ports") or [])
    pub = None
    for p in ports:
        if p.get("isIpPublic") and p.get("publicPort") and p.get("privatePort") == 22:
            pub = (p.get("ip"), p.get("publicPort"))
            break
    if not pub or not (key_path and os.path.exists(key_path)):
        return "unknown"
    ip, port = pub
    cmd = [
        "ssh", "-i", key_path, "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=6", "-o", "BatchMode=yes",
        f"root@{ip}",
        "pgrep -f 'wget|aria2c|curl' | wc -l",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return "unknown"
        return "downloading" if int(out.stdout.strip() or "0") > 0 else "idle"
    except Exception:
        return "unknown"


def heartbeat_age_min(path, now=None):
    if not path or not os.path.exists(path):
        return None  # missing file: driver never started or sandbox recycled
    return ((now or time.time()) - os.path.getmtime(path)) / 60.0


class Dog:
    def __init__(self, args):
        self.args = args
        self.rp = RunPod(get_key(args.key))
        self.idle_conf = {}  # pod_id -> consecutive idle confirmations

    def log(self, level, msg):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {level} {msg}"
        print(line, file=sys.stderr, flush=True)

    def audit_once(self):
        pods = self.rp.list_pods()
        report = []
        now = time.time()
        for pod in pods:
            pid = pod.get("id")
            rt = pod.get("runtime") or {}
            uptime = rt.get("uptimeInSeconds")
            entry = {
                "pod": pid,
                "name": pod.get("name"),
                "desired": pod.get("desiredStatus"),
                "cost_per_hr": pod.get("costPerHr"),
                "age_min": round(uptime / 60.0, 1) if uptime is not None else None,
                "running": uptime is not None,
            }
            if uptime is None:
                entry["verdict"] = "not_running_no_cost"
                self.idle_conf.pop(pid, None)
                report.append(entry)
                continue

            ttl = resolve_ttl_min(pod, self.args.default_ttl_min)
            entry["ttl_min"] = ttl
            age_min = uptime / 60.0
            if age_min > ttl:
                entry["verdict"] = "TTL_BREACH"
                entry["reason"] = f"age {entry['age_min']}m > ttl {ttl}m"
                if self.args.dry_run:
                    entry["action"] = "would-terminate"
                else:
                    try:
                        self.rp.terminate(pid)
                        entry["action"] = "TERMINATED"
                        self.log("KILL", f"{pid} ({pod.get('name')}): TTL breach "
                                         f"{entry['age_min']}m > {ttl}m -> TERMINATED")
                    except Exception as e:
                        entry["action"] = "terminate-failed"
                        entry["error"] = str(e)[:200]
                        self.log("ERROR", f"{pid}: terminate failed: {e}")
                self.idle_conf.pop(pid, None)
                report.append(entry)
                continue

            # IDLE rule — daemon only, never on GHA (no local heartbeat there).
            if self.args.mode == "daemon" and self.args.heartbeat_file:
                hb_age = heartbeat_age_min(self.args.heartbeat_file, now)
                queue = queue_probe(pod)
                dl = ssh_download_probe(pod, self.args.ssh_key) if self.args.ssh_key else "unknown"
                entry["hb_age_min"] = None if hb_age is None else round(hb_age, 1)
                entry["queue"] = queue
                entry["downloads"] = dl
                hb_stale = hb_age is None or hb_age > self.args.idle_min
                if hb_stale and queue == "empty" and dl != "downloading":
                    self.idle_conf[pid] = self.idle_conf.get(pid, 0) + 1
                else:
                    if self.idle_conf.get(pid, 0) > 0 and queue == "busy":
                        self.log("INFO", f"{pid}: active again (queue busy) — idle counter reset")
                    self.idle_conf.pop(pid, None)
                entry["idle_conf"] = self.idle_conf.get(pid, 0)
                if self.idle_conf.get(pid, 0) >= self.args.idle_confirmations:
                    entry["verdict"] = "IDLE"
                    entry["reason"] = (
                        f"heartbeat stale {entry['hb_age_min']}m (driver dead or never started), "
                        f"queue empty, downloads={dl}, confirmed {self.idle_conf[pid]}x"
                    )
                    if self.args.dry_run:
                        entry["action"] = "would-terminate"
                    else:
                        try:
                            self.rp.terminate(pid)
                            entry["action"] = "TERMINATED"
                            self.log("KILL", f"{pid}: IDLE -> TERMINATED ({entry['reason']})")
                        except Exception as e:
                            entry["action"] = "terminate-failed"
                            entry["error"] = str(e)[:200]
                            self.log("ERROR", f"{pid}: terminate failed: {e}")
                    self.idle_conf.pop(pid, None)
                    report.append(entry)
                    continue

            entry["verdict"] = "ok"
            report.append(entry)
        return report

    def emit(self, report):
        payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "mode": self.args.mode, "dry_run": self.args.dry_run,
                   "pods": report}
        if self.args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"kill-dog audit {payload['ts']} mode={self.args.mode}"
                  f" dry_run={self.args.dry_run} pods={len(report)}")
            for e in report:
                print(f"  {e['pod']}  {str(e.get('name'))[:24]:24} {e.get('verdict', '?'):20}"
                      f" age={e.get('age_min')} ttl={e.get('ttl_min')} hb={e.get('hb_age_min')}"
                      f" queue={e.get('queue')} dl={e.get('downloads')}"
                      f" {'[' + str(e.get('action')) + ']' if e.get('action') else ''}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as f:
                f.write(f"\n### kill-dog audit {payload['ts']}\n\n")
                f.write("| pod | name | verdict | age (min) | ttl | action |\n|---|---|---|---|---|---|\n")
                for e in report:
                    f.write(f"| {e['pod']} | {e.get('name')} | {e.get('verdict')} |"
                            f" {e.get('age_min')} | {e.get('ttl_min')} | {e.get('action', '')} |\n")

    def run_once(self):
        report = self.audit_once()
        self.emit(report)
        return 0

    def run_daemon(self):
        self.log("INFO", f"kill-dog daemon up: poll={self.args.poll_secs}s "
                         f"ttl_default={self.args.default_ttl_min}m idle={self.args.idle_min}m "
                         f"confirmations={self.args.idle_confirmations} hb={self.args.heartbeat_file}")
        fails = 0
        while True:
            try:
                report = self.audit_once()
                self.emit(report)
                fails = 0
            except KeyboardInterrupt:
                self.log("INFO", "interrupt — exit")
                return 0
            except Exception as e:
                fails += 1
                self.log("ERROR", f"audit failed ({fails} consecutive): {e}")
                if fails >= 30:
                    self.log("ERROR", "30 consecutive failures — daemon keeps trying (TTL is stateless)")
            time.sleep(self.args.poll_secs)


def main():
    ap = argparse.ArgumentParser(description="RunPod fleet kill-dog")
    ap.add_argument("--mode", choices=["once", "daemon"], default=None)
    ap.add_argument("--once", dest="mode_flag", action="store_const", const="once",
                    help="single audit pass (default)")
    ap.add_argument("--daemon", dest="mode_flag", action="store_const", const="daemon",
                    help="poll loop (use spawn_kill_dog.py, never a bare toolcall)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--key", default=None, help="RunPod API key (else env/file fallbacks)")
    ap.add_argument("--default-ttl-min", type=int, default=240,
                    help="TTL for pods without KILLDOG_TTL_MIN env (default 240)")
    ap.add_argument("--poll-secs", type=int, default=60)
    ap.add_argument("--idle-min", type=int, default=30,
                    help="heartbeat staleness (min) before pod counts as idle (daemon only)")
    ap.add_argument("--idle-confirmations", type=int, default=3)
    ap.add_argument("--heartbeat-file", default=None,
                    help="file the bench driver touches every poll; stale mtime = dead driver")
    ap.add_argument("--ssh-key", default=None, help="SSH private key to check for active downloads")
    args = ap.parse_args()
    if args.mode_flag:
        args.mode = args.mode_flag
    if not args.mode:
        args.mode = "once"

    dog = Dog(args)
    if args.mode == "daemon":
        sys.exit(dog.run_daemon())
    sys.exit(dog.run_once())


if __name__ == "__main__":
    main()
