"""Instance lifecycle: render -> place config -> start -> track -> stop.

Two records, so that "where does it run" never depends on the current lab file:

  On the node, per instance, in its work directory (all 0700/0600, configs can hold SIM keys):
    <run_dir>/<instance>/<instance>.cfg   generated config
    <run_dir>/<instance>/state.json       pid, argv, kernel start time, backend
    <run_dir>/<instance>/out.log          stdout+stderr (appended, one marker line per start)

  On the control machine, the run ledger ($GSM_LAB_STATE or ~/.local/state/gsm-lab, ledger/<lab>.json):
    instance -> node it was started on, that node's connection data AT START TIME, work directory, pid.

`status` and `stop` follow the ledger first, so they still reach the process after the instance was
moved to another node or removed from the lab file. `start` refuses to start a second copy while the
recorded one still runs elsewhere, or while its node cannot be reached to find out. "stopped" is only
reported after the process has been seen gone on the node it runs on.

A process is only ever signalled if /proc/<pid>/cmdline AND its start time still match state.json, so
gsm-lab cannot kill an unrelated process that happens to reuse a pid, nor processes it did not start.

Backends:
    proc  setsid'd background process (survives the shell/ssh session), default
    tmux  one pane per instance in session 'gsm-lab-<lab>' on the node (interactive), same tracking
"""

import hashlib
import json
import os
import shlex
import time
from types import SimpleNamespace

from .nodes import NodeError, NodeExec, executors
from .render import hlr_subscribers, render
from .topology import start_order
from .validate import _listeners


class LedgerError(Exception):
    pass


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

WINDOW = {"stp": "core", "hlr": "core", "mgw": "core", "msc": "core", "bsc": "ran", "bts": "ran",
          "trx": "ran", "virtphy": "ms", "mobile": "ms"}


# ---------------------------------------------------------------- control-machine run ledger
def control_dir():
    base = os.environ.get("GSM_LAB_STATE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "gsm-lab")
    return os.path.join(base, "ledger")


class Ledger:
    def __init__(self, lab_name):
        self.path = os.path.join(control_dir(), f"{lab_name}.json")

    def all(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise LedgerError(f"cannot read the run ledger {self.path}: {e}. It records where instances run; "
                              "fix or remove it only after checking the nodes by hand") from None
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        d = os.path.dirname(self.path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        tmp = f"{self.path}.tmp{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self, iid):
        return self.all().get(iid)

    def put(self, iid, rec):
        data = self.all()
        data[iid] = rec
        self._save(data)

    def drop(self, iid):
        data = self.all()
        if data.pop(iid, None) is not None:
            self._save(data)


def node_snapshot(n):
    return {"id": n.id, "host": n.host, "local": n.local, "ssh_user": n.ssh_user, "ssh_port": n.ssh_port,
            "ssh_options": list(n.ssh_options), "machine": n.machine}


class Runtime:
    def __init__(self, lab, backend=None, out=print):
        self.lab = lab
        self.ex = executors(lab)
        self.backend = backend or lab.backend
        self.out = out
        self.ledger = Ledger(lab.name)

    # ------------------------------------------------------------ where things are
    def placement(self, iid):
        """Where the CURRENT lab file puts the instance (None if it is not in the file / not placed)."""
        i = self.lab.instances.get(iid)
        if not i or i.node not in self.lab.nodes:
            return None
        return {"node": i.node, "conn": node_snapshot(self.lab.nodes[i.node]), "workdir": self.lab.instance_dir(i)}

    @staticmethod
    def same_place(a, b):
        """Same node AND same machine and work directory. The node id matters even on one machine: its
        addresses are part of the running instance's configuration."""
        return bool(a and b and a["node"] == b["node"] and a["conn"]["machine"] == b["conn"]["machine"]
                    and a["workdir"] == b["workdir"])

    def exec_for(self, loc):
        """Executor for a recorded location: the lab's node if it is still the same machine, otherwise the
        connection data recorded at start time (the node may have been changed or removed since)."""
        n = self.lab.nodes.get(loc["node"])
        if n and n.machine == loc["conn"]["machine"]:
            return self.ex[n.id]
        c = loc["conn"]
        return NodeExec(SimpleNamespace(id=c["id"], host=c["host"], local=c["local"], ssh_user=c["ssh_user"],
                                        ssh_port=c["ssh_port"], ssh_options=c["ssh_options"]))

    def probe(self, loc):
        """-> (state, pid) of the process recorded in loc's work directory."""
        try:
            _, out, _ = self.exec_for(loc).run(STATUS_SCRIPT % {"state": shlex.quote(loc["workdir"] + "/state.json")})
        except NodeError:
            return "unreachable", None
        parts = out.split()
        if not parts:
            return "unknown", None
        return parts[0], (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None)

    def where(self, iid):
        """-> (state, pid, loc). state: running | stopped | dead | foreign | unreachable | unknown.
        'running'/'unreachable' refer to the node the instance was STARTED on (loc), which can differ from
        the current placement; a recorded process is forgotten only once it is seen gone."""
        rec = self.ledger.get(iid)
        if rec:
            st, pid = self.probe(rec)
            if st in ("running", "unreachable", "unknown"):
                return st, pid, rec
            self.ledger.drop(iid)            # gone (or its pid now belongs to another program)
        cur = self.placement(iid)
        if cur is None:
            return ("dead", None, rec) if rec else ("stopped", None, None)
        if rec and self.same_place(rec, cur):
            return st, pid, cur
        st, pid = self.probe(cur)
        if st == "running":                  # started before the ledger existed, or by `exec`: adopt it
            self.ledger.put(iid, {**cur, "pid": pid, "service": self.lab.instances[iid].service,
                                  "adopted": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return st, pid, cur

    def status(self, iid):
        st, pid, _ = self.where(iid)
        return st, pid

    def removed(self):
        """Recorded instances that are no longer in the lab file."""
        return sorted(k for k in self.ledger.all() if k not in self.lab.instances)

    def misplaced(self, iid, loc):
        cur = self.placement(iid)
        return cur is not None and loc is not None and not self.same_place(loc, cur)

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
                    ex.run(f"cp -p {shlex.quote(launch.config_path)} {shlex.quote(backup)} && "
                           f"chmod 600 {shlex.quote(backup)}", check=True)
        if not dry_run:
            ex.write(launch.config_path, launch.config)
            ex.write(launch.config_path + ".sha256", hashlib.sha256(launch.config.encode()).hexdigest() + "\n")
        return note

    def prepare_dirs(self, launch):
        """run_dir and the instance directory 0700; existing contents (e.g. an hlr.db created before) made
        private too."""
        ex = self.ex[launch.node]
        run_dir = self.lab.nodes[launch.node].run_dir
        ex.mkdir_private(run_dir, launch.workdir, *[f"{launch.workdir}/{d}" for d in launch.dirs])
        ex.run(f"chmod -R go-rwx {shlex.quote(launch.workdir)}", check=True)

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
                if p_proto == proto and lo <= p_port <= hi and (p_ip == ip or "0.0.0.0" in (p_ip, ip)) and label != "rtp range":
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
        exe = shlex.quote(launch.argv[0])
        wait_exec = (f"for _ in 1 2 3 4 5 6 7 8 9 10; do [ \"$(tr '\\0' '\\n' < /proc/$pid/cmdline 2>/dev/null | head -1)\" = {exe} ] "
                     "&& break; sleep 0.1; done\n"
                     "st=$(awk '{print $22}' /proc/$pid/stat 2>/dev/null)\n"
                     "if [ -n \"$st\" ]; then echo $pid $st; else echo EXITED $pid; fi\n")
        if self.backend == "tmux":
            sess = shlex.quote(f"gsm-lab-{self.lab.name}")
            win = WINDOW[self.lab.instances[launch.instance].service]
            inner = shlex.quote(f"umask 077; cd {wd} && exec > >(tee -a out.log) 2>&1; echo {marker}; exec env {env} {cmd}")
            return (f"umask 077\ncd {wd} || exit 1\n"
                    f"if ! tmux has-session -t {sess} 2>/dev/null; then\n"
                    f"  pid=$(tmux new-session -d -s {sess} -n {win} -x 220 -y 60 -P -F '#{{pane_pid}}' bash -c {inner})\n"
                    f"  tmux set-option -t {sess} remain-on-exit on >/dev/null   # keep a crashed instance's output visible\n"
                    f"elif tmux list-windows -t {sess} -F '#{{window_name}}' | grep -qx {win}; then\n"
                    f"  pid=$(tmux split-window -t {sess}:{win} -P -F '#{{pane_pid}}' bash -c {inner})\n"
                    f"else\n"
                    f"  pid=$(tmux new-window -t {sess} -n {win} -P -F '#{{pane_pid}}' bash -c {inner})\n"
                    f"fi\n"
                    f"tmux select-layout -t {sess}:{win} tiled >/dev/null\n"
                    f"tmux select-pane -t {sess}:{win} -T {shlex.quote(launch.instance)} 2>/dev/null\n"
                    + wait_exec)
        return (f"umask 077\ncd {wd} || exit 1\necho {marker} >> out.log\n"
                f"env {env} setsid {cmd} </dev/null >>out.log 2>&1 &\npid=$!\n" + wait_exec)

    def refuse_reason(self, iid):
        """Why starting iid at its current placement would be unsafe, or None."""
        st, pid, loc = self.where(iid)
        if st == "running" and not self.same_place(loc, self.placement(iid)):
            return (f"still running on {loc['node']} ({loc['conn']['machine']}, pid {pid}), where it was started; "
                    f"the lab file now places it on {self.lab.instances[iid].node}. Stop it first: gsm-lab stop {iid}")
        if st in ("unreachable", "unknown"):
            return (f"recorded as running on {loc['node']} ({loc['conn']['machine']}), which cannot be reached to "
                    "check it; refusing to start a second copy. Check that node, then `gsm-lab stop` it")
        return None

    def start_one(self, iid, dry_run=False):
        """-> (message, pid or None). Never starts a second copy of a running instance."""
        reason = self.refuse_reason(iid)
        if reason:
            return f"REFUSED: {reason}", None
        st, pid = self.status(iid)
        if st == "running":
            return "already running", pid
        launch = render(self.lab, iid)
        if dry_run:
            return f"would start: {' '.join(launch.argv)}  (config: {self.place_config(launch, dry_run=True)})", None
        self.prepare_dirs(launch)
        note = self.place_config(launch)
        ex = self.ex[launch.node]
        rc, out, err = ex.run(self._launch_script(launch), timeout=20)
        parts = out.split()
        if parts[:1] == ["EXITED"]:
            return "exited immediately", None
        if rc != 0 or len(parts) < 2 or not parts[0].isdigit():
            return f"failed to launch: {err.strip()[-300:] or out.strip()[-300:]}", None
        pid, starttime = int(parts[0]), parts[1]
        self.record(iid, launch, pid, starttime)
        return f"started ({note or 'no config'})", pid

    def record(self, iid, launch, pid, starttime, backend=None):
        """state.json on the node + the ledger entry on the control machine."""
        self.ex[launch.node].write(f"{launch.workdir}/state.json", json.dumps({
            "pid": pid, "starttime": starttime, "argv": launch.argv, "backend": backend or self.backend,
            "lab": self.lab.name, "instance": iid, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
        self.ledger.put(iid, {**self.placement(iid), "pid": pid, "service": self.lab.instances[iid].service,
                              "started": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def stop_one(self, iid, timeout=5):
        st, pid, loc = self.where(iid)
        where = f" on {loc['node']}" if loc else ""
        if st == "stopped":
            return "not running"
        if st == "dead":
            return f"was not running (stale pid {pid}; process exited or was killed outside gsm-lab)"
        if st == "foreign":
            return f"NOT stopped: pid {pid}{where} now belongs to another program (left alone)"
        if st in ("unreachable", "unknown"):
            return f"NOT stopped: node {loc['node']} cannot be reached; still recorded as running there"
        ex = self.exec_for(loc)
        service = self.lab.instances[iid].service if iid in self.lab.instances else loc.get("service")
        # osmocom-bb mobile: SIGINT = shutdown with clean IMSI detach (its main.c); others: SIGTERM
        sig = "INT" if service == "mobile" else "TERM"
        note = "" if iid in self.lab.instances else " (removed from the lab file)"
        if self.misplaced(iid, loc):
            note += f" (it ran on {loc['node']}; the lab file now places it on {self.lab.instances[iid].node})"
        ex.run(f"kill -{sig} {int(pid)}")
        for _ in range(timeout * 10):
            st2, _ = self.probe(loc)
            if st2 not in ("running", "unreachable", "unknown"):
                self._forget(iid, loc, ex)
                return f"stopped (pid {int(pid)}{where}){note}"
            time.sleep(0.1)
        # still the same verified process: escalate
        ex.run(f"kill -KILL {int(pid)}")
        time.sleep(0.3)
        if self.probe(loc)[0] == "running":
            return f"NOT stopped: pid {int(pid)}{where} survived SIGKILL"
        self._forget(iid, loc, ex)
        return f"killed after {timeout}s (pid {int(pid)}{where} did not exit on SIG{sig}){note}"

    def _forget(self, iid, loc, ex):
        """After a verified stop: no state file (so status says 'not started', not 'exited'), no ledger entry."""
        ex.run(f"rm -f {shlex.quote(loc['workdir'] + '/state.json')}")
        self.ledger.drop(iid)

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
        for gone in self.removed():
            self.out(f"[WARN] {gone}: removed from the lab file but still recorded as running "
                     f"(`gsm-lab status`); stop it with `gsm-lab stop {gone}`")
        probe_cache, failed, results = {}, set(), []
        subs = hlr_subscribers(self.lab)
        for iid in order:
            blocked = sorted(d for d in deps[iid] if d in failed)
            if blocked:
                failed.add(iid)
                results.append((iid, "blocked", f"not started: depends on failed {', '.join(blocked)}"))
                self.out(f"[SKIP] {iid}: depends on failed {', '.join(blocked)}")
                continue
            reason = self.refuse_reason(iid)
            if reason:
                failed.add(iid)
                results.append((iid, "refused", reason))
                self.out(f"[FAIL] {iid}: REFUSED: {reason}")
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
                self.out(f"[FAIL] {iid}: {msg}" + ("" if msg.startswith("REFUSED") else "; last output:"))
                if not msg.startswith("REFUSED"):
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
        """Stop instances (default: all in the lab file AND all recorded ones removed from it since)."""
        order, deps = start_order(self.lab)
        removed = self.removed()
        want = set(targets) if targets else set(self.lab.instances) | set(removed)
        if targets and with_dependents:    # stopping a server also stops what depends on it
            changed = True
            while changed:
                changed = False
                for k, d in deps.items():
                    if k not in want and d & want:
                        want.add(k)
                        changed = True
        res = []
        for iid in [k for k in removed if k in want] + [k for k in reversed(order) if k in want]:
            msg = self.stop_one(iid)
            res.append((iid, msg))
            self.out(f"{iid:12} {msg}")
        if not targets:   # full stop: also remove this lab's own tmux session (dead panes), whatever the backend
            sess = shlex.quote('gsm-lab-' + self.lab.name)
            for ex in {n.machine: self.ex[n.id] for n in self.lab.nodes.values()}.values():
                try:
                    ex.run(f"command -v tmux >/dev/null && tmux kill-session -t {sess} 2>/dev/null")
                except NodeError:
                    pass
        return res

    # ------------------------------------------------------------ HLR provisioning (idempotent)
    def provision(self, hlr_id, subscribers):
        """Ki values travel inside VTY commands on stdin (NodeExec.vty), never as process arguments."""
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
        """Output of the instance, from the node it actually runs (ran) on."""
        rec = self.ledger.get(iid)
        loc = rec or self.placement(iid)
        if loc is None:
            return ""
        try:
            _, out, _ = self.exec_for(loc).run(f"tail -n {int(n)} {shlex.quote(loc['workdir'] + '/out.log')} 2>/dev/null")
        except NodeError as e:
            return f"(cannot read log: {e})\n"
        return out
