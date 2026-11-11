"""Instance lifecycle: render -> place config -> start -> track -> stop.

Process tracking is per instance, in its work directory on its node:
    <run_dir>/<instance>/<instance>.cfg   generated config
    <run_dir>/<instance>/state.json       pid, argv, kernel start time, backend
    <run_dir>/<instance>/out.log          stdout+stderr (appended, one marker line per start)

A process is only ever signalled if /proc/<pid>/cmdline AND its start time still match state.json, so
gsm-lab cannot kill an unrelated process that happens to reuse a pid, nor processes it did not start.

Backends:
    proc  setsid'd background process (survives the shell/ssh session), default
    tmux  one pane per instance in session 'gsm-lab-<lab>' on the node (interactive), same tracking
"""

import hashlib
import json
import shlex
import time

from .nodes import NodeError, executors
from .render import hlr_subscribers, render
from .topology import start_order
from .validate import _listeners


def _state_path(launch):
    return f"{launch.workdir}/state.json"


STATUS_SCRIPT = r"""
f=%(state)s
[ -f "$f" ] || { echo stopped; exit; }
pid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$f" 2>/dev/null)
st=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["starttime"])' "$f" 2>/dev/null)
exe=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["argv"][0])' "$f" 2>/dev/null)
[ -n "$pid" ] && [ -d /proc/$pid ] || { echo "dead $pid"; exit; }
cur=$(awk '{print $22}' /proc/$pid/stat 2>/dev/null)
cmd0=$(tr '\0' '\n' < /proc/$pid/cmdline | head -1)
if [ "$cur" = "$st" ] && [ "$cmd0" = "$exe" ]; then echo "running $pid"; else echo "foreign $pid"; fi
"""


class Runtime:
    def __init__(self, lab, backend=None, out=print):
        self.lab = lab
        self.ex = executors(lab)
        self.backend = backend or lab.backend
        self.out = out

    # ------------------------------------------------------------ status
    def status(self, iid):
        """-> (state, pid): running | stopped | dead (crashed / killed outside gsm-lab) | foreign (pid reused)"""
        launch = render(self.lab, iid)
        rc, out, _ = self.ex[launch.node].run(STATUS_SCRIPT % {"state": shlex.quote(_state_path(launch))})
        parts = out.split()
        if not parts:
            return "unknown", None
        return parts[0], (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None)

    # ------------------------------------------------------------ config placement (with drift backup)
    def place_config(self, launch, dry_run=False):
        """Write the generated config. If the file on the node was changed by hand since gsm-lab wrote it,
        keep that version as <cfg>.edited-<timestamp> (never silently overwrite user changes)."""
        if launch.config is None:
            return None
        ex = self.ex[launch.node]
        cur = ex.read(launch.config_path)
        if cur == launch.config:
            return "unchanged"
        note = "new" if cur is None else "updated"
        if cur is not None:
            rec = ex.read(launch.config_path + ".sha256")
            if not rec or hashlib.sha256(cur.encode()).hexdigest() != rec.strip():
                backup = f"{launch.config_path}.edited-{time.strftime('%Y%m%d-%H%M%S')}"
                note = f"hand-edited copy kept as {backup}"
                if not dry_run:
                    ex.run(f"cp -p {shlex.quote(launch.config_path)} {shlex.quote(backup)}", check=True)
        if not dry_run:
            ex.write(launch.config_path, launch.config)
            ex.write(launch.config_path + ".sha256", hashlib.sha256(launch.config.encode()).hexdigest() + "\n")
        return note

    # ------------------------------------------------------------ preflight for one instance
    def preflight(self, iid, probe_cache):
        """-> list of problem strings; empty if the instance can be started."""
        i = self.lab.instances[iid]
        launch = render(self.lab, iid)
        node = self.lab.nodes[i.node]
        problems = []
        if node.id not in probe_cache:
            try:
                probe_cache[node.id] = self.ex[node.id].probe(
                    sorted({render(self.lab, x).argv[0] for x, ii in self.lab.instances.items() if ii.node == node.id}))
            except NodeError as e:
                probe_cache[node.id] = e
        pr = probe_cache[node.id]
        if isinstance(pr, NodeError):
            return [f"node {node.id} unreachable: {pr}"]
        if not pr["bins"].get(launch.argv[0], False):
            problems.append(f"binary missing on {node.id}: {launch.argv[0]} (run ./gsm build there, or set nodes.{node.id}.prefix)")
        addrs = set(pr["addr"])
        for inst, label, _, proto, ip, lo, hi in _listeners(self.lab):
            if inst.id != iid:
                continue
            if ip and ip != "0.0.0.0" and not ip.startswith("127.") and ip not in addrs:
                problems.append(f"{label}: address {ip} does not exist on {node.id} (has: {', '.join(sorted(addrs))})")
            for p_proto, p_ip, p_port in pr["listen"]:
                pp = {"tcp": "tcp", "udp": "udp", "sctp": "sctp"}.get(p_proto, p_proto)
                if pp == proto and lo <= p_port <= hi and (p_ip == ip or "0.0.0.0" in (p_ip, ip)) and label != "rtp range":
                    problems.append(f"{label}: {proto} {ip}:{p_port} already in use on {node.id}")
        if i.service == "msc" and not pr["sctp"]:
            problems.append(f"kernel SCTP not available on {node.id} (osmo-msc opens an SCTP SGs listener): sudo modprobe sctp")
        if self.backend == "tmux" and not pr["tools"].get("tmux"):
            problems.append(f"tmux not installed on {node.id}")
        if not pr["tools"].get("python3"):
            problems.append(f"python3 missing on {node.id} (used for process tracking and VTY checks)")
        return problems

    # ------------------------------------------------------------ start / stop one
    def _launch_script(self, launch):
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in launch.env.items())
        cmd = " ".join(shlex.quote(a) for a in launch.argv)
        wd = shlex.quote(launch.workdir)
        marker = shlex.quote(f"==== gsm-lab start {launch.instance} " + " ".join(launch.argv))
        if self.backend == "tmux":
            sess = shlex.quote(f"gsm-lab-{self.lab.name}")
            win = {"stp": "core", "hlr": "core", "mgw": "core", "msc": "core", "bsc": "ran", "bts": "ran",
                   "trx": "ran", "virtphy": "ms", "mobile": "ms"}[self.lab.instances[launch.instance].service]
            inner = f"cd {wd} && exec > >(tee -a out.log) 2>&1; echo {marker}; exec env {env} {cmd}"
            title = shlex.quote(launch.instance)
            return f"""
cd {wd} || exit 1
if ! tmux has-session -t {sess} 2>/dev/null; then
  pid=$(tmux new-session -d -s {sess} -n {win} -x 220 -y 60 -P -F '#{{pane_pid}}' bash -c {shlex.quote(inner)})
  tmux set-option -t {sess} remain-on-exit on >/dev/null   # keep a crashed instance's last output visible
elif tmux list-windows -t {sess} -F '#{{window_name}}' | grep -qx {win}; then
  pid=$(tmux split-window -t {sess}:{win} -P -F '#{{pane_pid}}' bash -c {shlex.quote(inner)})
else
  pid=$(tmux new-window -t {sess} -n {win} -P -F '#{{pane_pid}}' bash -c {shlex.quote(inner)})
fi
tmux select-layout -t {sess}:{win} tiled >/dev/null
tmux select-pane -t {sess}:{win} -T {title} 2>/dev/null
# the pane runs bash, which execs into the program after setting up the tee
for _ in 1 2 3 4 5 6 7 8 9 10; do [ "$(tr '\\0' '\\n' < /proc/$pid/cmdline 2>/dev/null | head -1)" = {shlex.quote(launch.argv[0])} ] && break; sleep 0.1; done
st=$(awk '{{print $22}}' /proc/$pid/stat 2>/dev/null)
if [ -n "$st" ]; then echo $pid $st; else echo EXITED $pid; fi
"""
        return f"""
cd {wd} || exit 1
echo {marker} >> out.log
env {env} setsid {cmd} </dev/null >>out.log 2>&1 &
pid=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ "$(tr '\\0' '\\n' < /proc/$pid/cmdline 2>/dev/null | head -1)" = {shlex.quote(launch.argv[0])} ] && break; sleep 0.05; done
st=$(awk '{{print $22}}' /proc/$pid/stat 2>/dev/null)
if [ -n "$st" ]; then echo $pid $st; else echo EXITED $pid; fi
"""

    def start_one(self, iid, dry_run=False):
        launch = render(self.lab, iid)
        state, pid = self.status(iid)
        if state == "running":
            return "already running", pid
        note = self.place_config(launch, dry_run=dry_run)
        if dry_run:
            return f"would start: {' '.join(launch.argv)}  (config: {note})", None
        ex = self.ex[launch.node]
        ex.run("mkdir -p " + " ".join(shlex.quote(f"{launch.workdir}/{d}") for d in [""] + launch.dirs), check=True)
        rc, out, err = ex.run(self._launch_script(launch), timeout=20)
        parts = out.split()
        if parts[:1] == ["EXITED"]:
            return "exited immediately", None
        if rc != 0 or len(parts) < 2 or not parts[0].isdigit():
            return f"failed to launch: {err.strip()[-300:] or out.strip()[-300:]}", None
        pid, starttime = int(parts[0]), parts[1]
        ex.write(_state_path(launch), json.dumps({
            "pid": pid, "starttime": starttime, "argv": launch.argv, "backend": self.backend,
            "lab": self.lab.name, "instance": iid, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
        return f"started ({note or 'no config'})", pid

    def stop_one(self, iid, timeout=5):
        launch = render(self.lab, iid)
        state, pid = self.status(iid)
        if state == "stopped":
            return "not running"
        if state == "dead":
            return f"was not running (stale pid {pid}; process exited or was killed outside gsm-lab)"
        if state == "foreign":
            return f"NOT stopped: pid {pid} now belongs to another program (left alone)"
        if state != "running":
            return f"unknown state {state}"
        ex = self.ex[launch.node]
        # osmocom-bb mobile: SIGINT = shutdown with clean IMSI detach (its main.c); others: SIGTERM
        sig = "INT" if self.lab.instances[iid].service == "mobile" else "TERM"
        ex.run(f"kill -{sig} {pid}")
        for _ in range(timeout * 10):
            if self.status(iid)[0] != "running":
                return f"stopped (pid {pid})"
            time.sleep(0.1)
        # still the same verified process: escalate
        ex.run(f"kill -KILL {pid}")
        time.sleep(0.3)
        return f"killed after {timeout}s (pid {pid} did not exit on SIG{sig})"

    # ------------------------------------------------------------ orchestration
    def wait_vty(self, iid, timeout=15):
        i = self.lab.instances[iid]
        if i.vty is None:
            time.sleep(0.5)
            return self.status(iid)[0] == "running"
        ex = self.ex[i.node]
        end = time.time() + timeout
        while time.time() < end:
            if self.status(iid)[0] != "running":
                return False
            if ex.vty(i.vty.port, [], ip=i.vty.ip, timeout=1) is not None:
                return True
            time.sleep(0.5)
        return False

    def start(self, targets=None, dry_run=False, with_deps=True):
        """Start instances in dependency order. A failed instance blocks only its dependents."""
        order, deps = start_order(self.lab)
        enabled = {k for k, i in self.lab.instances.items() if i.enabled}
        want = set(targets) if targets else enabled
        if targets and with_deps:
            stack = list(want)
            while stack:
                k = stack.pop()
                for d in deps.get(k, ()):
                    if d not in want:
                        want.add(d)
                        stack.append(d)
        order = [k for k in order if k in want]
        probe_cache, failed, results = {}, set(), []
        subs = hlr_subscribers(self.lab)
        for iid in order:
            blocked = sorted(d for d in deps[iid] if d in failed)
            if blocked:
                failed.add(iid)
                results.append((iid, "blocked", f"not started: depends on failed {', '.join(blocked)}"))
                self.out(f"[SKIP] {iid}: depends on failed {', '.join(blocked)}")
                continue
            state, pid = self.status(iid)
            if state == "running":
                results.append((iid, "ok", f"already running (pid {pid})"))
                self.out(f"[ OK ] {iid}: already running (pid {pid})")
                continue
            if state == "foreign":
                self.out(f"[WARN] {iid}: stale state file, pid {pid} now belongs to another program (ignored)")
            problems = self.preflight(iid, probe_cache)
            if problems:
                failed.add(iid)
                results.append((iid, "fail", "; ".join(problems)))
                self.out(f"[FAIL] {iid}: preflight")
                for p in problems:
                    self.out(f"       -> {p}")
                continue
            msg, pid = self.start_one(iid, dry_run=dry_run)
            if dry_run:
                results.append((iid, "dry-run", msg))
                self.out(f"[DRY ] {iid} on {self.lab.instances[iid].node}: {msg}")
                continue
            if pid is None:
                failed.add(iid)
                results.append((iid, "fail", msg))
                self.out(f"[FAIL] {iid}: {msg}; last output:")
                for line in self.log_tail(iid, 8).splitlines():
                    self.out(f"       | {line}")
                continue
            if not self.wait_vty(iid):
                failed.add(iid)
                tail = self.log_tail(iid, 8)
                results.append((iid, "fail", "process exited or VTY did not answer"))
                self.out(f"[FAIL] {iid}: {msg}, but " + ("exited" if self.status(iid)[0] != "running" else "VTY not answering"))
                for line in tail.splitlines():
                    self.out(f"       | {line}")
                continue
            results.append((iid, "ok", f"{msg}, pid {pid}"))
            self.out(f"[ OK ] {iid} on {self.lab.instances[iid].node}: {msg}, pid {pid}")
            if iid in subs and subs[iid]:
                self.provision(iid, subs[iid])
        return results, failed

    def stop(self, targets=None, with_dependents=True):
        order, deps = start_order(self.lab)
        want = set(targets) if targets else set(self.lab.instances)
        if targets and with_dependents:    # stopping a server also stops what depends on it
            changed = True
            while changed:
                changed = False
                for k, d in deps.items():
                    if k not in want and d & want:
                        want.add(k)
                        changed = True
        res = []
        for iid in reversed(order):
            if iid in want:
                msg = self.stop_one(iid)
                res.append((iid, msg))
                self.out(f"{iid:12} {msg}")
        if not targets:   # full stop: also remove this lab's own tmux session (dead panes), whatever the backend
            sess = shlex.quote('gsm-lab-' + self.lab.name)
            for ex in {n.machine: self.ex[n.id] for n in self.lab.nodes.values()}.values():
                ex.run(f"command -v tmux >/dev/null && tmux kill-session -t {sess} 2>/dev/null")
        return res

    # ------------------------------------------------------------ HLR provisioning (idempotent)
    def provision(self, hlr_id, subscribers):
        i = self.lab.instances[hlr_id]
        ex = self.ex[i.node]
        for s in subscribers:
            imsi = str(s["imsi"])
            cmds = [f"subscriber imsi {imsi} create"]
            if s.get("msisdn"):
                cmds.append(f"subscriber imsi {imsi} update msisdn {s['msisdn']}")
            if s.get("ki"):
                cmds.append(f"subscriber imsi {imsi} update aud2g comp128v1 ki {s['ki']}")
            # 'create' fails harmlessly if the subscriber exists; the updates then make it match the lab.
            # Retried: right after start the HLR VTY can answer before its database is ready.
            for attempt in range(5):
                out = ex.vty(i.vty.port, cmds + [f"subscriber imsi {imsi} show"], ip=i.vty.ip, timeout=10)
                ok = out is not None and f"IMSI: {imsi}" in out and (
                    not s.get("ki") or f"ki={str(s['ki']).lower()}" in out.lower())
                if ok:
                    break
                time.sleep(1)
            if not ok:
                errs = [l for l in (out or "no VTY answer").splitlines() if l.startswith("%") or l == "no VTY answer"]
                self.out(f"       -> {'; '.join(errs[-3:])}")
            self.out(f"       {'provisioned' if ok else 'provisioning FAILED for'} {imsi} on {hlr_id}"
                     + (f" (MSISDN {s['msisdn']})" if s.get("msisdn") else ""))

    def log_tail(self, iid, n=40):
        launch = render(self.lab, iid)
        _, out, _ = self.ex[launch.node].run(f"tail -n {int(n)} {shlex.quote(launch.workdir + '/out.log')} 2>/dev/null")
        return out
