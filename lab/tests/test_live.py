"""Live tests against the real Osmocom binaries in _install (no radio: virtual BTS + virtual phone).

Opt-in, because they start real network elements and need the default ports free:
    GSM_LAB_LIVE=1 python3 -m unittest lab/tests/test_live.py -v

Each test starts a topology with `gsm-lab start`, requires every health check (process, VTY, links) and
the end-to-end check (authenticated location update + SMS to own MSISDN) to pass, then stops it.
Multi-node topologies run with overrides/one-host.yaml: every node is local with its own loopback
address, so all inter-node endpoints are real sockets on distinct addresses.
"""

import os
import subprocess
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GSM_LAB = os.path.join(ROOT, "gsm-lab")
TOPO = os.path.join(ROOT, "lab", "topologies")
ONE_HOST = os.path.join(TOPO, "overrides", "one-host.yaml")


def run(*args, timeout=300):
    p = subprocess.run([GSM_LAB, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


@unittest.skipUnless(os.environ.get("GSM_LAB_LIVE") == "1", "set GSM_LAB_LIVE=1 to run live Osmocom tests")
class Live(unittest.TestCase):
    def lab(self, name, *extra):
        base = ["-f", os.path.join(TOPO, name), *extra]
        rc, out = run(*base, "start")
        try:
            self.assertEqual(rc, 0, out)
            time.sleep(12)
            rc, out = run(*base, "health", "--e2e", "--timeout", "90")
            self.assertEqual(rc, 0, out)
            self.assertIn("Result: OK", out)
            return out
        finally:
            run(*base, "stop")

    def test_single_node(self):
        out = self.lab("single-node.yaml")
        self.assertIn("RSL up on 1/1 TRX", out)

    def test_one_msc_two_bscs_three_nodes(self):
        out = self.lab("multi-bsc.yaml", "-o", ONE_HOST, "-d", "bsc-per-node")
        self.assertIn("3 ASP(s) active", out)
        self.assertIn("RSL up on 2/2 TRX", out)

    def test_bscs_colocated_bts_distributed(self):
        self.lab("multi-bsc.yaml", "-o", ONE_HOST, "-d", "core-bsc")

    def test_two_plmns_one_node(self):
        out = self.lab("multi-plmn.yaml", "-o", ONE_HOST)
        self.assertEqual(out.count("delivered (MO via MSC, MT back to the phone)"), 2)


if __name__ == "__main__":
    unittest.main()
