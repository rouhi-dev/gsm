"""Regression tests for the pre-merge hardening pass (review of PR #1).

Runtime cases use real processes (the stand-in binary from test_runtime) and, for "remote" nodes, the
`ssh` shim. The shim runs gsm-lab's exact remote command lines locally: it proves command construction,
quoting, stdin transport and file modes on the remote code path -- NOT real SSH to another machine.
"""

import copy
import os
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from helpers import base, build, codes, issues
from test_runtime import FakeOsmocomCase, rd
from gsmlab import health, nodes, render, repo
from gsmlab.runtime import Ledger, Runtime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GSM_LAB = os.path.join(ROOT, "gsm-lab")
QUIET = dict(out=lambda *a: None)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ================================================================================ P1.1 run ledger
class RunLedger(FakeOsmocomCase):
    def two_node_raw(self):
        raw = base()
        raw["nodes"]["n2"] = {"host": "localhost", "run_dir": os.path.join(self.tmp, "run-n2")}
        return raw

    def test_moving_a_running_instance_keeps_tracking_it(self):
        raw = self.two_node_raw()
        lab1 = self.lab(copy.deepcopy(raw))
        rt1 = Runtime(lab1, **QUIET)
        _, pid = rt1.start_one("stp")
        raw["instances"]["stp"]["node"] = "n2"                 # the lab file now places it elsewhere
        lab2 = self.lab(raw)
        rt2 = Runtime(lab2, **QUIET)
        st, p, loc = rt2.where("stp")
        self.assertEqual((st, p, loc["node"]), ("running", pid, "n1"), "must never look stopped while it runs")
        msg, newpid = rt2.start_one("stp")
        self.assertIsNone(newpid)
        self.assertIn("REFUSED", msg)
        self.assertIn("still running on n1", msg)
        results, failed = rt2.start(["stp"], with_deps=False)
        self.assertIn("stp", failed)
        self.assertEqual(results[0][1], "refused")
        checks = [c for c in health.check(rt2, ["stp"]) if c.level == "process"]
        self.assertFalse(checks[0].ok)
        self.assertIn("places it on n2", checks[0].detail)
        out = rt2.stop_one("stp")                               # reaches the ORIGINAL node
        self.assertIn("stopped", out)
        self.assertIn("on n1", out)
        self.assertFalse(os.path.exists(f"/proc/{pid}"))
        self.assertEqual(Ledger("t").all(), {})
        msg, pid2 = rt2.start_one("stp")                        # now it can start at the new place
        self.assertTrue(msg.startswith("started"), msg)
        self.assertEqual(rt2.where("stp")[2]["node"], "n2")
        rt2.stop_one("stp")

    def test_move_between_nodes_of_one_machine_with_same_run_dir(self):
        """One host, two node identities, same work directory path: still a move (addresses differ)."""
        raw = base()
        raw["nodes"]["n2"] = {"host": "localhost", "address": "127.0.2.1"}
        rt1 = Runtime(self.lab(copy.deepcopy(raw)), **QUIET)
        _, pid = rt1.start_one("stp")
        raw["instances"]["stp"]["node"] = "n2"
        rt2 = Runtime(self.lab(raw), **QUIET)
        self.assertIn("still running on n1", rt2.start_one("stp")[0])
        self.assertTrue(rt2.misplaced("stp", rt2.where("stp")[2]))
        self.assertIn("stopped", rt2.stop_one("stp"))
        self.assertFalse(os.path.exists(f"/proc/{pid}"))

    def test_removed_running_instance_is_reported_and_stopped(self):
        raw = base()
        lab1 = self.lab(copy.deepcopy(raw))
        rt1 = Runtime(lab1, **QUIET)
        _, pid = rt1.start_one("mgw")
        del raw["instances"]["mgw"]                              # removed from the lab file while running
        lab2 = self.lab(raw)
        rt2 = Runtime(lab2, **QUIET)
        self.assertEqual(rt2.removed(), ["mgw"])
        removed = [c for c in health.check(rt2) if c.instance == "mgw"]
        self.assertTrue(removed and not removed[0].ok and "REMOVED" in removed[0].detail)
        res = dict(rt2.stop())                                    # a full stop includes it
        self.assertIn("removed from the lab file", res["mgw"])
        self.assertFalse(os.path.exists(f"/proc/{pid}"))
        self.assertEqual(rt2.removed(), [])

    def test_unreachable_recorded_node_is_never_reported_stopped(self):
        self.ssh_shim()
        raw = base()
        raw["nodes"]["n1"] = {"host": "bbu-a", "address": "127.0.0.1", "ssh": {"user": "lab"},
                              "home": self.tmp}
        lab = self.lab(raw)
        rt = Runtime(lab, **QUIET)
        _, pid = rt.start_one("stp")
        self.ssh_shim(fail=True)                                  # node goes away
        self.assertEqual(rt.status("stp")[0], "unreachable")
        self.assertIn("NOT stopped", rt.stop_one("stp"))
        self.assertIn("REFUSED", rt.start_one("stp")[0])
        self.assertTrue(os.path.exists(f"/proc/{pid}"))
        self.assertIn("stp", Ledger("t").all())                   # still recorded
        self.ssh_shim()                                           # node is back
        self.assertIn("stopped", rt.stop_one("stp"))
        self.assertFalse(os.path.exists(f"/proc/{pid}"))

    def test_stop_uses_the_connection_recorded_at_start(self):
        self.ssh_shim()
        raw = base()
        raw["nodes"]["n1"] = {"host": "bbu-a", "address": "127.0.0.1", "ssh": {"user": "lab"}, "home": self.tmp}
        rt1 = Runtime(self.lab(copy.deepcopy(raw)), **QUIET)
        _, pid = rt1.start_one("stp")
        raw["nodes"]["n1"]["host"] = "bbu-b"                      # node definition changed afterwards
        rt2 = Runtime(self.lab(raw), **QUIET)
        open(self.ssh_log, "w").close()
        self.assertIn("stopped", rt2.stop_one("stp"))
        self.assertFalse(os.path.exists(f"/proc/{pid}"))
        calls = rd(self.ssh_log)
        self.assertIn("lab@bbu-a", calls)
        self.assertNotIn("bbu-b", calls.split("kill")[0].splitlines()[-1] if "kill" in calls else calls)

    def test_cli_stop_accepts_removed_instance(self):
        raw = base()
        rt1 = Runtime(self.lab(copy.deepcopy(raw)), **QUIET)
        _, pid = rt1.start_one("mgw")
        del raw["instances"]["mgw"]
        raw["networks"]["net"]["mgw"] = None
        raw["lab"].update({"run_dir": os.path.join(self.tmp, "run"), "prefix": self.prefix})
        f = os.path.join(self.tmp, "lab.yaml")
        import yaml
        with open(f, "w") as fh:
            yaml.safe_dump(raw, fh)
        p = subprocess.run([GSM_LAB, "-f", f, "stop", "mgw"], capture_output=True, text=True, env=os.environ)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        self.assertIn("removed from the lab file", p.stdout)
        self.assertFalse(os.path.exists(f"/proc/{pid}"))


# ================================================================================ P2.2 permissions
class Permissions(FakeOsmocomCase):
    def check_private(self, lab, iid="hlr"):
        node = lab.nodes["n1"]
        wd = lab.instance_dir(lab.instances[iid])
        self.assertEqual(mode(node.run_dir), 0o700)
        self.assertEqual(mode(wd), 0o700)
        for f in (f"{iid}.cfg", f"{iid}.cfg.sha256", "state.json", "out.log"):
            self.assertEqual(mode(os.path.join(wd, f)), 0o600, f)
        led = Ledger(lab.name).path
        self.assertEqual(mode(led), 0o600)
        self.assertEqual(mode(os.path.dirname(led)), 0o700)

    def test_local_node(self):
        lab = self.lab()
        wd = lab.instance_dir(lab.instances["hlr"])
        os.makedirs(wd)
        with open(os.path.join(wd, "hlr.db"), "w") as f:          # left world-readable by an earlier run
            f.write("x")
        os.chmod(os.path.join(wd, "hlr.db"), 0o644)
        rt = Runtime(lab, **QUIET)
        rt.start_one("hlr")
        self.check_private(lab)
        self.assertEqual(mode(os.path.join(wd, "hlr.db")), 0o600)
        rt.stop_one("hlr")

    def test_remote_node_via_shim(self):
        self.ssh_shim()
        raw = base()
        raw["nodes"]["n1"] = {"host": "bbu-a", "address": "127.0.0.1", "ssh": {"user": "lab"}, "home": self.tmp}
        old = os.umask(0o002)                                       # permissive umask must not matter
        try:
            lab = self.lab(raw)
            rt = Runtime(lab, **QUIET)
            rt.start_one("hlr")
            self.check_private(lab)
            rt.stop_one("hlr")
        finally:
            os.umask(old)

    def test_hand_edit_backup_is_private(self):
        lab = self.lab()
        rt = Runtime(lab, **QUIET)
        l = render.render(lab, "stp")
        rt.prepare_dirs(l)
        rt.place_config(l)
        os.chmod(l.config_path, 0o644)
        with open(l.config_path, "a") as f:
            f.write("! edit\n")
        rt.place_config(l)
        b = [x for x in os.listdir(l.workdir) if ".edited-" in x][0]
        self.assertEqual(mode(os.path.join(l.workdir, b)), 0o600)


# ================================================================================ P2.1 Ki never in argv
class FakeVty(threading.Thread):
    """Minimal VTY server: answers every line with a prompt and records what it received."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.received = []

    def run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            c.sendall(b"Fake> ")
            buf = b""
            while True:
                d = c.recv(4096)
                if not d:
                    break
                buf += d
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    self.received.append(line.decode())
                    c.sendall(b"Fake# ")
            c.close()


class KiNotInArguments(FakeOsmocomCase):
    KI = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

    def test_vty_commands_go_over_stdin_local_and_ssh(self):
        captured = []

        def fake_run(argv, **kw):
            captured.append((argv, kw.get("input")))
            return subprocess.CompletedProcess(argv, 0, "", "")

        for node in ({"host": "localhost"}, {"host": "bbu-a", "address": "127.0.0.1", "ssh": {"user": "lab"}}):
            raw = base()
            raw["nodes"]["n1"] = node
            ex = nodes.NodeExec(build(raw).nodes["n1"])
            with mock.patch.object(nodes.subprocess, "run", side_effect=fake_run):
                ex.vty(4258, [f"subscriber imsi 001010000000001 update aud2g comp128v1 ki {self.KI}"])
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[1][0][0], "ssh")
        for argv, stdin in captured:
            self.assertFalse(any(self.KI in a for a in argv), argv)
            self.assertIn(self.KI, stdin)

    def test_provisioning_over_ssh_shim_keeps_ki_off_command_lines(self):
        self.ssh_shim()
        srv = FakeVty()
        srv.start()
        self.addCleanup(srv.sock.close)
        raw = base()
        raw["nodes"]["n1"] = {"host": "bbu-a", "address": "127.0.0.1", "ssh": {"user": "lab"}, "home": self.tmp}
        raw["instances"]["hlr"]["vty"] = srv.port
        lab = self.lab(raw)
        Runtime(lab, **QUIET).provision("hlr", [{"imsi": "001010000000001", "ki": self.KI}])
        self.assertTrue(any(self.KI in r for r in srv.received), "the Ki must reach the VTY (via stdin)")
        self.assertNotIn(self.KI, rd(self.ssh_log), "the Ki must not appear on the ssh command line")


# ================================================================================ P1.2 orphans
class Orphans(unittest.TestCase):
    def orphan_raw(self):
        raw = base()
        raw["instances"].update({
            "msc2": {"service": "msc", "node": "n1", "point_code": "0.23.7", "bind": "127.0.0.2"},
            "bsc2": {"service": "bsc", "node": "n1", "point_code": "0.23.6", "bind": "127.0.0.3"},
            "bts2": {"service": "bts", "variant": "virtual", "node": "n1", "vty": 4271, "ctrl": 4272},
            "trx9": {"service": "trx", "node": "n1"}})
        return raw

    def test_orphans_are_errors(self):
        errs = [i for i in issues(self.orphan_raw()) if i.code == "INST-ORPHAN"]
        self.assertEqual(sorted(i.path for i in errs),
                         ["instances.bsc2", "instances.bts2", "instances.msc2", "instances.trx9"])

    def test_render_raises_clean_error(self):
        lab = build(self.orphan_raw())
        for iid in ("msc2", "bsc2", "bts2", "trx9"):
            with self.assertRaises(render.RenderError) as cm:
                render.render(lab, iid)
            self.assertIn(iid, str(cm.exception))

    def test_cli_never_shows_a_traceback(self):
        import yaml
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "lab.yaml")
            raw = self.orphan_raw()
            raw["lab"]["run_dir"] = d
            with open(f, "w") as fh:
                yaml.safe_dump(raw, fh)
            env = {**os.environ, "GSM_LAB_STATE": os.path.join(d, "state")}
            for args, must_fail in ((["render"], True), (["start", "--dry-run"], True), (["render", "msc2"], True),
                                    (["validate"], True), (["topology", "--links"], False), (["services"], False)):
                p = subprocess.run([GSM_LAB, "-f", f, *args], capture_output=True, text=True, env=env)
                self.assertNotIn("Traceback", p.stdout + p.stderr, args)
                if must_fail:
                    self.assertNotEqual(p.returncode, 0, args)
                    self.assertIn("INST-ORPHAN", p.stdout + p.stderr, args)


# ================================================================================ P1.3 injection
class Injection(unittest.TestCase):
    def assertRejected(self, mutate, code="SCHEMA"):
        raw = base()
        mutate(raw)
        self.assertIn(code, codes(raw))

    def test_newlines_and_control_characters_rejected(self):
        self.assertRejected(lambda r: r["networks"]["net"].update(short_name="Lab\nmsc\n mncc external /tmp/x"))
        self.assertRejected(lambda r: r["networks"]["net"].update(encryption="a5 0\r"))
        self.assertRejected(lambda r: r["networks"]["net"]["bscs"]["bsc"]["bts"]["bts1"].update(band="DCS1800\x1b[2J"))
        self.assertRejected(lambda r: r["plmns"]["p"].update(name="A B"))
        self.assertRejected(lambda r: r["instances"]["bsc"].update(args=["-d", "DLINP\n:1"]))
        self.assertRejected(lambda r: r["instances"]["bsc"].update(raw="network\n bad \x07"))
        self.assertRejected(lambda r: r["instances"].update({"bts\nx": {"service": "bts", "node": "n1"}}))
        self.assertRejected(lambda r: r["nodes"].update({"n1": {"host": "localhost", "run_dir": "/tmp/a\nb"}}))

    def test_multiline_raw_is_allowed(self):
        raw = base()
        raw["instances"]["bsc"]["raw"] = "network\n\tneci 1\n"
        self.assertEqual(codes(raw), [])
        self.assertIn("neci 1", render.render(build(raw), "bsc").config)

    def test_identifiers_hosts_users(self):
        self.assertRejected(lambda r: r["instances"].update({"has space": {"service": "stp", "node": "n1"}}))
        self.assertRejected(lambda r: r["nodes"].update({"n9": {"host": "-oProxyCommand=touch /tmp/x", "address": "10.0.0.9"}}))
        self.assertRejected(lambda r: r["nodes"].update({"n9": {"host": "h", "address": "10.0.0.9", "ssh": {"user": "a b"}}}))
        self.assertRejected(lambda r: r["nodes"].update({"n9": {"host": "h", "address": "10.0.0.9", "ssh": {"options": "-v"}}}))

    def test_rendered_output_check(self):
        with self.assertRaises(render.RenderError):
            render.check_rendered("line vty\n bind 127.0.0.1\x00\n")

    def test_tcp_probe_accepts_only_ip_and_port(self):
        ex = nodes.NodeExec(build(base()).nodes["n1"])
        with self.assertRaises(ValueError):
            ex.tcp_reachable("127.0.0.1/$(touch /tmp/x)", 1)
        with self.assertRaises(ValueError):
            ex.vty(4242, ["show\nx"])


# ================================================================================ P1.4 per-service keys
class ServiceKeys(unittest.TestCase):
    def rejected(self, mutate):
        raw = base()
        mutate(raw)
        return [i for i in issues(raw) if i.level == "error"]

    def test_unsupported_keys_rejected(self):
        cases = [
            lambda r: r["instances"]["msc"].update(sms_db="/x/sms.db"),            # was accepted + ignored
            lambda r: r["instances"]["msc"].update(remote="10.0.0.1"),             # was accepted + ignored
            lambda r: r["networks"]["net"]["bscs"]["bsc"]["bts"]["bts1"].update(neighbors=["x"]),
            lambda r: r["networks"]["net"]["bscs"]["bsc"]["bts"]["bts1"].update(opts={}),
            lambda r: r["instances"]["bts1"].update(point_code="0.23.5"),
            lambda r: r["instances"]["stp"].update(ctrl=4300),                     # osmo-stp has no CTRL
            lambda r: r["instances"]["bsc"].update(virtual_um={}),
            lambda r: r["instances"]["bts1"].update(trx_options={"local_base_port": 5800}),   # trx variant only
            lambda r: r["instances"]["msc"].update(ports={"sgs": {"port": 1, "proto": "tcp"}}),
            lambda r: r["instances"]["hlr"].update(subscribers=[{"imsi": "001010000000001", "opc": "x"}]),
            lambda r: r["instances"]["msc"].update(log_level="loud"),
            lambda r: r["instances"]["stp"].update(transport="udp"),
            lambda r: r["instances"].update(phy={"service": "virtphy", "node": "n1", "raw": "x"}),
            lambda r: r["instances"].update(phy={"service": "virtphy", "node": "n1", "virtual_um": {"bts_port": 1}}),
            lambda r: r["instances"].update(t={"service": "trx", "node": "n1", "trx_options": {"Bad Key": 1}}),
            lambda r: r["instances"].update(ms={"service": "mobile", "node": "n1", "virtphy": "x", "ctrl": 1,
                                                 "sims": [{"imsi": "001010000000001"}]}),
        ]
        for n, m in enumerate(cases):
            self.assertTrue(self.rejected(m), f"case {n} was accepted")

    def test_virtphy_ttl_is_implemented(self):
        raw = base()
        raw["instances"]["phy"] = {"service": "virtphy", "node": "n1", "virtual_um": {"ttl": 3}}
        self.assertEqual(codes(raw), [])
        argv = render.render(build(raw), "phy").argv
        self.assertEqual(argv[argv.index("-T") + 1], "3")


# ================================================================================ P1.5 placeholders, VTY exposure
class Warnings(unittest.TestCase):
    def test_testnet_addresses_are_flagged_as_placeholders(self):
        raw = base()
        raw["nodes"]["n2"] = {"host": "198.51.100.12", "address": "198.51.100.12", "ssh": {"user": "lab"}}
        self.assertIn("NODE-PLACEHOLDER", codes(raw, "warn"))
        raw["nodes"]["n2"] = {"host": "10.20.0.12", "address": "10.20.0.12", "ssh": {"user": "lab"}}
        self.assertNotIn("NODE-PLACEHOLDER", codes(raw, "warn"))

    def test_examples_use_only_documentation_addresses(self):
        import re
        for root, _, files in os.walk(os.path.join(ROOT, "lab")):
            for f in files:
                if f.endswith((".yaml", ".md")):
                    text = rd(os.path.join(root, f))
                    self.assertNotRegex(text, r"\b192\.168\.\d+\.\d+", f)
                    for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
                        if not ip.startswith(("127.", "0.", "239.", "198.51.100.", "192.0.2.", "203.0.113.")) \
                                and not re.fullmatch(r"\d\.\d+\.\d+\.\d+", ip):   # point codes like 0.23.1
                            self.fail(f"{f}: non-documentation address {ip}")

    def test_exposed_vty_ctrl_warned(self):
        raw = base()
        raw["nodes"]["n1"]["mgmt_address"] = "10.0.0.1"
        raw["nodes"]["n1"]["address"] = "10.0.0.1"
        w = [i for i in issues(raw) if i.code == "MGMT-EXPOSED"]
        self.assertTrue(w and all(i.level == "warn" for i in w))
        self.assertNotIn("MGMT-EXPOSED", codes(base(), "warn"))


# ================================================================================ P2.3 / P2.4 git
class GitBehaviour(unittest.TestCase):
    def test_local_override_files_are_ignored_everywhere(self):
        paths = ["lab/lab.local.yaml", "lab/topologies/overrides/site.local.yaml", "lab/x/y/z.local.yaml",
                 "other/a.local.yaml"]
        p = subprocess.run(["git", "check-ignore", "--no-index", *paths], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(sorted(p.stdout.split()), sorted(paths))
        p = subprocess.run(["git", "check-ignore", "--no-index", "lab/topologies/overrides/one-host.yaml"],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, "committed override examples must not be ignored")

    def test_user_ssh_command_is_respected(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}):
                os.environ.pop("GIT_SSH_COMMAND", None)
                os.environ.pop("GIT_SSH", None)
                self.assertEqual(repo.git_env(d)["GIT_SSH_COMMAND"], repo.BATCH_SSH)
                subprocess.run(["git", "-C", d, "config", "core.sshCommand", "ssh -i /k/my_key"], check=True)
                self.assertNotIn("GIT_SSH_COMMAND", repo.git_env(d))
                p = subprocess.run(["git", "-C", d, "config", "--get", "core.sshCommand"], capture_output=True,
                                   text=True, env=repo.git_env(d))
                self.assertEqual(p.stdout.strip(), "ssh -i /k/my_key")
                os.environ["GIT_SSH_COMMAND"] = "ssh -F /my/config"
                self.assertEqual(repo.git_env(d)["GIT_SSH_COMMAND"], "ssh -F /my/config")
            self.assertEqual(repo.git_env(d)["GIT_TERMINAL_PROMPT"], "0")


if __name__ == "__main__":
    unittest.main()
