"""Runtime behaviour without Osmocom: real processes (a tiny compiled stand-in), real files, and the remote
(SSH) code path through an `ssh` shim that runs the remote command locally.

What this proves: lifecycle (start/status/stop), process ownership checks, config drift backup, dependency
blocking, and that the exact command lines gsm-lab sends over SSH work. What it does not prove: real SSH
transport/authentication to another machine (no sshd here) -- see docs/LAB.md 'Verification status'.
"""

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

from helpers import base, build
from gsmlab import nodes
from gsmlab.runtime import Runtime

FAKE_C = r"""
#include <signal.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    /* stand-in for an Osmocom program: prints its arguments, then waits for a signal */
    for (int i = 0; i < argc; i++) printf("%s ", argv[i]);
    printf("\n"); fflush(stdout);
    if (argc > 3 && strcmp(argv[3], "--die") == 0) return 1;
    for (;;) pause();
}
"""


def rd(path):
    with open(path) as f:
        return f.read()


def wjson(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f)


def have_cc():
    return shutil.which("cc") is not None


@unittest.skipUnless(have_cc(), "needs a C compiler for the stand-in binary")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gsm-lab-rt-")
        self.prefix = os.path.join(self.tmp, "prefix")
        os.makedirs(os.path.join(self.prefix, "bin"))
        src = os.path.join(self.tmp, "fake.c")
        with open(src, "w") as f:
            f.write(FAKE_C)
        fake = os.path.join(self.prefix, "bin", "fake")
        subprocess.check_call(["cc", "-o", fake, src])
        for b in ("osmo-stp", "osmo-hlr", "osmo-mgw", "osmo-msc", "osmo-bsc", "osmo-bts-virtual"):
            os.link(fake, os.path.join(self.prefix, "bin", b))
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        # stop anything a test left running (only processes recorded by gsm-lab state files)
        for root, _, files in os.walk(self.tmp):
            if "state.json" in files:
                try:
                    pid = json.loads(rd(os.path.join(root, "state.json")))["pid"]
                    if os.readlink(f"/proc/{pid}/exe").startswith(self.prefix):
                        os.kill(pid, signal.SIGKILL)
                except (OSError, ValueError, KeyError):
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lab(self, raw=None, **node):
        raw = raw or base()
        raw["lab"]["run_dir"] = os.path.join(self.tmp, "run")
        raw["lab"]["prefix"] = self.prefix
        raw["nodes"]["n1"].update(node)
        return build(raw)

    # ------------------------------------------------------------------------------------------
    def test_start_status_stop_cycle(self):
        lab = self.lab()
        rt = Runtime(lab, out=lambda *a: None)
        msg, pid = rt.start_one("stp")
        self.assertTrue(msg.startswith("started"), msg)
        self.assertEqual(rt.status("stp"), ("running", pid))
        self.assertEqual(rt.start_one("stp")[0], "already running")        # idempotent
        cfg = os.path.join(self.tmp, "run", "stp", "stp.cfg")
        self.assertIn("listen m3ua 2905 tcp", rd(cfg))
        self.assertIn("==== gsm-lab start stp", rd(os.path.join(self.tmp, "run", "stp", "out.log")))
        self.assertTrue(rt.stop_one("stp").startswith("stopped"))
        self.assertEqual(rt.status("stp")[0], "dead")                       # state file kept, process gone
        self.assertFalse(os.path.exists(f"/proc/{pid}"))

    def test_never_kills_a_process_it_did_not_start(self):
        lab = self.lab()
        rt = Runtime(lab, out=lambda *a: None)
        victim = subprocess.Popen(["sleep", "300"])
        self.procs.append(victim)
        wd = os.path.join(self.tmp, "run", "stp")
        os.makedirs(wd)
        st = rd(f"/proc/{victim.pid}/stat").rsplit(")", 1)[1].split()[19]
        # a stale state file whose pid now belongs to an unrelated program
        wjson({"pid": victim.pid, "starttime": st, "argv": [self.prefix + "/bin/osmo-stp"]},
              os.path.join(wd, "state.json"))
        self.assertEqual(rt.status("stp")[0], "foreign")
        self.assertIn("NOT stopped", rt.stop_one("stp"))
        time.sleep(0.2)
        self.assertIsNone(victim.poll(), "unrelated process must survive")
        # right program name but different start time (pid reuse): still not ours
        wjson({"pid": victim.pid, "starttime": "1", "argv": ["sleep"]}, os.path.join(wd, "state.json"))
        self.assertEqual(rt.status("stp")[0], "foreign")

    def test_hand_edited_config_is_backed_up(self):
        lab = self.lab()
        rt = Runtime(lab, out=lambda *a: None)
        from gsmlab.render import render
        l = render(lab, "stp")
        os.makedirs(l.workdir)
        self.assertEqual(rt.place_config(l), "new")
        self.assertEqual(rt.place_config(l), "unchanged")
        with open(l.config_path, "a") as f:
            f.write("! my manual tweak\n")
        note = rt.place_config(l)
        self.assertIn("hand-edited copy kept", note)
        backups = [f for f in os.listdir(l.workdir) if ".edited-" in f]
        self.assertEqual(len(backups), 1)
        self.assertIn("my manual tweak", rd(os.path.join(l.workdir, backups[0])))
        self.assertEqual(rd(l.config_path), l.config)
        # gsm-lab's own previous version is replaced without a backup
        lab2 = self.lab(raw=dict(base(), lab={"name": "t", "run_dir": "x"}))
        lab2.instances["stp"].log_level = "debug"
        self.assertEqual(rt.place_config(render(lab2, "stp")), "updated")

    def test_failure_blocks_only_dependents(self):
        raw = base()
        raw["instances"]["hlr"]["args"] = ["--die"]          # stand-in exits at once: like a config error
        lab = self.lab(raw)
        rt = Runtime(lab, out=lambda *a: None)
        rt.wait_vty = lambda iid, timeout=15: rt.status(iid)[0] == "running"   # stand-in has no VTY
        results, failed = rt.start(["msc"])                  # pulls in its dependencies stp, hlr, mgw
        status = {i: s for i, s, _ in results}
        self.assertEqual(status["hlr"], "fail")
        self.assertEqual(status["msc"], "blocked")
        self.assertEqual(status["stp"], "ok")
        self.assertNotIn("bsc", status)                      # not requested, not a dependency
        rt.stop()
        self.assertEqual(rt.status("stp")[0], "dead")

    def test_dry_run_changes_nothing(self):
        lab = self.lab()
        rt = Runtime(lab, out=lambda *a: None)
        rt.start(dry_run=True)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "run", "stp", "stp.cfg")))

    # ------------------------------------------------------------------------------------------
    def test_remote_node_via_ssh_shim(self):
        """Same lifecycle on a 'remote' node: every action goes through `ssh ... bash -c <script>`."""
        shim_dir = os.path.join(self.tmp, "shim")
        os.makedirs(shim_dir)
        log = os.path.join(self.tmp, "ssh.log")
        shim = os.path.join(shim_dir, "ssh")
        with open(shim, "w") as f:   # records the target, then runs the remote command line locally
            f.write(f'#!/bin/bash\nfor a; do last="$a"; done\n'
                    f'printf "%s\\n" "$*" | head -c 300 >> {log}; echo >> {log}\n'
                    f'exec bash -c "$last"\n')
        os.chmod(shim, stat.S_IRWXU)
        old = os.environ["PATH"]
        os.environ["PATH"] = shim_dir + os.pathsep + old
        try:
            lab = self.lab(host="bbu-07.lab", address="127.0.0.1", ssh={"user": "lab", "port": 2222})
            n = lab.nodes["n1"]
            self.assertFalse(n.local)
            ex = nodes.NodeExec(n)
            tricky = "it's \"quoted\" $HOME `x` \\ ok\n"
            p = os.path.join(self.tmp, "remote file.txt")
            ex.write(p, tricky)
            self.assertEqual(ex.read(p), tricky)
            self.assertIn("ok /bin/sh", "\n".join(f"ok {b}" for b, ok in ex.probe(["/bin/sh"])["bins"].items() if ok))
            rt = Runtime(lab, out=lambda *a: None)
            msg, pid = rt.start_one("stp")
            self.assertTrue(msg.startswith("started"), msg)
            self.assertEqual(rt.status("stp")[0], "running")
            self.assertTrue(rt.stop_one("stp").startswith("stopped"))
            calls = rd(log)
            self.assertIn("-p 2222", calls)
            self.assertIn("lab@bbu-07.lab", calls)
            self.assertIn("BatchMode=yes", calls)
        finally:
            os.environ["PATH"] = old

    def test_unreachable_node_is_reported(self):
        shim_dir = os.path.join(self.tmp, "shim")
        os.makedirs(shim_dir)
        with open(os.path.join(shim_dir, "ssh"), "w") as f:
            f.write("#!/bin/bash\necho 'ssh: connect to host bbu-99 port 22: No route to host' >&2\nexit 255\n")
        os.chmod(os.path.join(shim_dir, "ssh"), stat.S_IRWXU)
        old = os.environ["PATH"]
        os.environ["PATH"] = shim_dir + os.pathsep + old
        try:
            lab = self.lab(host="bbu-99", address="10.1.1.1")
            ok, info = nodes.ping_node(nodes.NodeExec(lab.nodes["n1"]))
            self.assertFalse(ok)
            self.assertIn("No route to host", info)
            rt = Runtime(lab, out=lambda *a: None)
            probs = rt.preflight("stp", {})
            self.assertTrue(probs and "unreachable" in probs[0], probs)
        finally:
            os.environ["PATH"] = old


if __name__ == "__main__":
    unittest.main()
