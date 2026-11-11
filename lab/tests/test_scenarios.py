"""Architecture scenarios (configuration level): what gsm-lab accepts, rejects, and generates.

Each test names the scenario number of the project brief. These tests need no Osmocom binaries; the live
counterparts are in test_live.py (GSM_LAB_LIVE=1).
"""

import re
import unittest

from helpers import base, build, cfg, codes, issues, load
from gsmlab import render, topology, validate


def add_node(raw, nid, address, host=None):
    raw["nodes"][nid] = {"host": host or address, "address": address, "ssh": {"user": "lab"}}


class Scenarios(unittest.TestCase):
    # 1 ---------------------------------------------------------------------------------------------
    def test_01_single_node_complete_network(self):
        lab = load("single-node.yaml")
        self.assertEqual([i for i in validate.validate(lab) if i.level == "error"], [])
        msc = render.render(lab, "msc-01").config
        self.assertIn("network country code 001\n mobile network code 01\n", msc)
        self.assertIn("authentication required", msc)
        order, _ = topology.start_order(lab)
        self.assertLess(order.index("stp-01"), order.index("msc-01"))
        self.assertLess(order.index("msc-01"), order.index("bsc-01"))
        self.assertLess(order.index("bsc-01"), order.index("bts-01"))
        self.assertLess(order.index("phy-ms1"), order.index("ms-01"))

    # 2 ---------------------------------------------------------------------------------------------
    def test_02_core_bsc_bts_on_three_nodes(self):
        lab = load("multi-bsc.yaml", "split")
        self.assertEqual([i for i in validate.validate(lab) if i.level == "error"], [])
        bsc = render.render(lab, "bsc-01").config
        # the BSC (node-02) reaches STP and MGW on node-01 -- never localhost
        self.assertRegex(bsc, r"asp asp-bsc-01 2905 0 m3ua tcp\n  remote-ip 192\.168\.50\.11\n  local-ip 192\.168\.50\.12")
        self.assertRegex(bsc, r"mgw 0\n  remote-ip 192\.168\.50\.11\n  remote-port 2427\n  local-ip 192\.168\.50\.12")
        self.assertIn("ipa bind 192.168.50.12", bsc)
        bts = render.render(lab, "bts-01").config
        self.assertIn("oml remote-ip 192.168.50.12", bts)           # BTS on node-03 -> BSC on node-02

    # 3 ---------------------------------------------------------------------------------------------
    def test_03_one_msc_two_bscs(self):
        lab = load("multi-bsc.yaml", "bsc-per-node")
        for b in ("bsc-01", "bsc-02"):
            c = render.render(lab, b).config
            self.assertIn("sccp-address msc-msc-01\n  routing-indicator PC\n  point-code 0.23.1", c)
            self.assertIn("msc 0\n msc-addr msc-msc-01", c)
        self.assertEqual([m for m, _ in topology.ss7_domains(lab)["stp-01"]], ["msc-01", "bsc-01", "bsc-02"])
        # a BSC without an MSC in its network is rejected
        raw = base()
        del raw["networks"]["net"]["msc"]
        self.assertIn("REF-MISSING", codes(raw))

    # 4 ---------------------------------------------------------------------------------------------
    def test_04_several_bts_per_bsc(self):
        c = render.render(load("multi-bsc.yaml"), "bsc-01").config
        self.assertIn(" bts 0\n  type osmo-bts\n  band DCS1800\n  location_area_code 10\n  cell_identity 101", c)
        self.assertIn(" bts 1\n  type osmo-bts\n  band DCS1800\n  location_area_code 10\n  cell_identity 102", c)
        self.assertIn("ipa unit-id 1001 0", c)
        self.assertIn("ipa unit-id 1002 0", c)

    # 5 ---------------------------------------------------------------------------------------------
    def test_05_bts_of_one_bsc_on_different_nodes(self):
        lab = load("multi-bsc.yaml", "bsc-per-node")      # bts-01 on node-02, bts-02 on node-03, both bsc-01
        self.assertEqual(lab.instances["bts-01"].node, "node-02")
        self.assertEqual(lab.instances["bts-02"].node, "node-03")
        for b in ("bts-01", "bts-02"):
            self.assertIn("oml remote-ip 192.168.50.12", render.render(lab, b).config)
        # ... and a BSC Abis listener on loopback cannot serve a remote BTS
        raw = base()
        add_node(raw, "n2", "10.0.0.2")
        raw["instances"]["bts1"]["node"] = "n2"
        self.assertIn("LINK-LOOPBACK", codes(raw))

    # 6 ---------------------------------------------------------------------------------------------
    def test_06_several_trx_per_bts(self):
        lab = load("multi-bsc.yaml")
        bsc = render.render(lab, "bsc-01").config
        self.assertIn("  trx 1\n   rf_locked 0\n   arfcn 875", bsc)
        self.assertIn("   rsl e1 tei 1\n", bsc)
        bts = render.render(lab, "bts-01").config
        self.assertIn(" instance 0\n instance 1\n", bts)
        self.assertIn(" trx 1\n  phy 0 instance 1", bts)
        sdr = load("sdr-b210.yaml")
        trx = render.render(sdr, "trx-01").config
        self.assertIn(" chan 0\n chan 1\n", trx)
        # TRXs of one BTS must share one osmo-trx; channels unique; ARFCN in band
        raw = base()
        raw["instances"]["bts1"]["variant"] = "trx"
        raw["instances"]["t1"] = {"service": "trx", "node": "n1"}
        raw["networks"]["net"]["bscs"]["bsc"]["bts"]["bts1"]["trx"] = [
            {"arfcn": 871, "phy": "t1", "channel": 0}, {"arfcn": 2000, "phy": "t1", "channel": 0}]
        self.assertTrue({"TRX-CHAN", "TRX-ARFCN"} <= set(codes(raw)))

    # 7 ---------------------------------------------------------------------------------------------
    def test_07_independent_networks(self):
        lab = load("multi-plmn.yaml")
        self.assertEqual(set(lab.networks), {"net-a", "net-b"})
        self.assertEqual(set(topology.ss7_domains(lab)), {"a-stp", "b-stp"})
        # identical point codes are fine in separate SS7 domains (two STPs) ...
        self.assertEqual([i for i in validate.validate(lab) if i.level == "error"], [])

    # 8 ---------------------------------------------------------------------------------------------
    def test_08_plmns_with_separate_infrastructure(self):
        lab = load("multi-plmn.yaml")
        self.assertIn("network country code 001\n mobile network code 002\n", render.render(lab, "b-msc").config)
        self.assertIn("network country code 001\n mobile network code 002\n", render.render(lab, "b-bsc").config)
        self.assertIn("network country code 001\n mobile network code 01\n", render.render(lab, "a-bsc").config)
        subs = render.hlr_subscribers(lab)       # SIMs go to the HLR of their home PLMN (IMSI prefix)
        self.assertEqual([s["imsi"] for s in subs["a-hlr"]], ["001010000000301"])
        self.assertEqual([s["imsi"] for s in subs["b-hlr"]], ["001002000000401"])

    # 9 ---------------------------------------------------------------------------------------------
    def test_09_plmns_on_one_node_and_capability_limits(self):
        lab = load("multi-plmn.yaml")
        self.assertEqual({i.node for i in lab.instances.values()}, {"node-01"})
        raw = base()                                   # one MSC cannot serve two PLMNs
        raw["plmns"]["q"] = {"mcc": "001", "mnc": "02"}
        raw["instances"]["bsc2"] = {"service": "bsc", "node": "n1", "point_code": "0.23.4", "bind": "127.0.0.2"}
        raw["networks"]["net2"] = {"plmn": "q", "stp": "stp", "msc": "msc", "hlr": "hlr", "mgw": "mgw",
                                   "bscs": {"bsc2": {}}}
        self.assertIn("CAP-MSC-PLMN", codes(raw))
        raw = base()                                   # one BSC cannot be in two networks
        raw["networks"]["net2"] = dict(raw["networks"]["net"], bscs={"bsc": {}})
        self.assertIn("CAP-BSC-NETS", codes(raw))
        raw = base()                                   # BSC broadcasting another PLMN = special config, warned
        raw["plmns"]["q"] = {"mcc": "001", "mnc": "02"}
        raw["networks"]["net"]["bscs"]["bsc"]["ran_plmn"] = "q"
        self.assertIn("PLMN-REWRITE", codes(raw, "warn"))
        c = cfg(raw, "bsc")
        self.assertIn("network country code 001\n mobile network code 02\n", c)
        self.assertIn("core-mobile-country-code 001\n core-mobile-network-code 01\n", c)
        raw = base()                                   # shared HLR across PLMNs: warned, not hidden
        raw["plmns"]["q"] = {"mcc": "001", "mnc": "02"}
        raw["instances"].update({"msc2": {"service": "msc", "node": "n1", "point_code": "0.23.5", "vty": 4354,
                                          "ctrl": 4355, "bind": "127.0.0.2"}})
        raw["networks"]["net2"] = {"plmn": "q", "stp": "stp", "msc": "msc2", "hlr": "hlr", "mgw": "mgw"}
        self.assertIn("CAP-HLR-SHARED", codes(raw, "warn"))

    # 10 --------------------------------------------------------------------------------------------
    def test_10_several_instances_of_one_service_on_one_node(self):
        raw = base()
        raw["instances"]["bts2"] = {"service": "bts", "variant": "virtual", "node": "n1"}
        raw["networks"]["net"]["bscs"]["bsc"]["bts"]["bts2"] = {"lac": 1, "ci": 2, "unit_id": 101, "trx": [{"arfcn": 873}]}
        self.assertIn("PORT-CONFLICT", codes(raw))          # default VTY/CTRL ports collide ...
        raw["instances"]["bts2"].update({"vty": 4271, "ctrl": 4272})
        self.assertEqual(codes(raw), [])                     # ... explicit ports resolve it
        lab = build(raw)
        self.assertNotEqual(lab.instance_dir(lab.instances["bts1"]), lab.instance_dir(lab.instances["bts2"]))
        self.assertIn("pcu-socket /tmp/gsm-lab-test/t/bts2/pcu_bts", cfg(raw, "bts2"))  # no shared /tmp/pcu_bts

    # 11 --------------------------------------------------------------------------------------------
    def test_11_move_bts_between_nodes_keeps_identity(self):
        cells = {}
        for dep in ("all-in-one", "split", "core-bsc", "bsc-per-node"):
            lab = load("multi-bsc.yaml", dep)
            self.assertEqual([i for i in validate.validate(lab) if i.level == "error"], [], dep)
            c = render.render(lab, "bsc-01").config
            cells[dep] = re.search(r"\n bts 0\n.*?(?=\nmsc 0)", c, re.S).group(0)
        self.assertEqual(len(set(cells.values())), 1, "cell identity must not depend on placement")
        self.assertEqual(load("multi-bsc.yaml", "split").instances["bts-01"].node, "node-03")
        self.assertEqual(load("multi-bsc.yaml", "core-bsc").instances["bts-01"].node, "node-02")

    # 12 --------------------------------------------------------------------------------------------
    def test_12_missing_node_and_unreachable_endpoint(self):
        raw = base()
        raw["instances"]["bsc"]["node"] = "nope"
        self.assertIn("INST-NODE", codes(raw))
        raw = base()
        add_node(raw, "n2", "10.0.0.2")
        raw["instances"]["msc"].update({"node": "n2", "bind": "10.0.0.2"})   # STP/HLR/MGW stay on loopback
        self.assertIn("LINK-LOOPBACK", codes(raw))
        raw = base()
        raw["nodes"]["far"] = {"host": "10.9.9.9"}                          # remote node without address
        self.assertIn("NODE-ADDR", codes(raw))
        self.assertIn("SCHEMA", codes({**base(), "lab": {"name": "t", "deployment": "missing"}}))

    # 13 --------------------------------------------------------------------------------------------
    def test_13_port_conflicts(self):
        raw = base()
        raw["instances"]["mgw2"] = {"service": "mgw", "node": "n1", "vty": 4343, "ctrl": 4367,
                                    "rtp_range": [16000, 17000]}
        e = [i for i in issues(raw) if i.code == "PORT-CONFLICT"]
        self.assertTrue(any("mgcp" in i.message for i in e))
        self.assertTrue(any("rtp range" in i.message for i in e))       # 16000-16001 overlaps 4002-16001
        raw = base()
        raw["instances"]["hlr"]["ports"] = {"gsup": 4223}                   # fixed in osmo-hlr 1.9.4
        self.assertIn("PORT-FIXED", codes(raw))
        raw = base()
        raw["instances"]["bsc2"] = {"service": "bsc", "node": "n1", "point_code": "0.23.4", "vty": 4342, "ctrl": 4349,
                                    "ports": {"mgcp_client": 2729}}
        self.assertTrue(any("abis" in i.message for i in issues(raw) if i.code == "PORT-CONFLICT"))
        raw["instances"]["bsc2"]["bind"] = "127.0.0.2"                      # second IP: allowed
        self.assertFalse(any("abis" in i.message for i in issues(raw) if i.code == "PORT-CONFLICT"))

    # 14 --------------------------------------------------------------------------------------------
    def test_14_invalid_plmn_and_signalling(self):
        for plmn, code in (({"mcc": 1, "mnc": "01"}, "SCHEMA"), ({"mcc": "01", "mnc": "01"}, "PLMN-MCC"),
                           ({"mcc": "001", "mnc": "1"}, "PLMN-MNC"),
                           ({"mcc": "001", "mnc": "01", "mnc_length": 3}, "PLMN-MNC")):
            raw = base()
            raw["plmns"]["p"] = plmn
            self.assertIn(code, codes(raw), plmn)
        raw = base()
        raw["plmns"]["dup"] = {"mcc": "001", "mnc": "01"}
        self.assertIn("PLMN-DUP", codes(raw))
        raw = base()
        raw["instances"]["bsc"]["point_code"] = "0.23.1"                    # same as the MSC, same STP
        self.assertIn("SS7-PC-DUP", codes(raw))
        raw = base()
        raw["instances"]["bsc"]["point_code"] = "9.999.9"
        self.assertIn("SS7-PC", codes(raw))
        raw = base()
        raw["networks"]["net"]["routing"] = "static"                       # needs fixed ASP ports
        self.assertIn("SS7-STATIC", codes(raw))
        raw["instances"]["msc"]["m3ua_local_port"] = 2911
        raw["instances"]["bsc"]["m3ua_local_port"] = 2913
        self.assertEqual(codes(raw), [])
        stp = cfg(raw, "stp")
        self.assertIn("routing-key 1 0.23.1", stp)
        self.assertIn("update route 0.23.3 7.255.7 linkset as-bsc", stp)
        self.assertNotIn("dynamic-permitted", stp)

    # 15 --------------------------------------------------------------------------------------------
    def test_15_invalid_topology_references(self):
        raw = base()
        raw["networks"]["net"]["plmn"] = "nope"
        raw["networks"]["net"]["msc"] = "hlr"                               # wrong service type
        raw["networks"]["net"]["bscs"]["ghost"] = {}
        self.assertTrue({"REF-PLMN", "REF-TYPE", "REF-UNKNOWN"} <= set(codes(raw)))
        raw = base()                                                         # BTS used twice
        raw["instances"]["bsc2"] = {"service": "bsc", "node": "n1", "point_code": "0.23.4", "bind": "127.0.0.2"}
        raw["networks"]["net"]["bscs"]["bsc2"] = {"bts": {"bts1": {"lac": 2, "ci": 1, "unit_id": 5, "trx": [{"arfcn": 880}]}}}
        self.assertIn("BTS-DUP", codes(raw))
        raw = base()                                                         # duplicate unit id / LAC+CI
        raw["instances"]["bts2"] = {"service": "bts", "variant": "virtual", "node": "n1", "vty": 4271, "ctrl": 4272}
        raw["networks"]["net"]["bscs"]["bsc"]["bts"]["bts2"] = {"lac": 1, "ci": 1, "unit_id": 100, "trx": [{"arfcn": 873}]}
        self.assertTrue({"BTS-UNIT", "CELL-DUP"} <= set(codes(raw)))
        raw = base()
        raw["instances"]["bsc"]["colour"] = "red"                            # typos are reported, not ignored
        self.assertIn("SCHEMA", codes(raw))


class Loading(unittest.TestCase):
    def test_mcc_mnc_stay_strings(self):
        lab = load("multi-plmn.yaml")
        self.assertEqual((lab.plmns["operator_b"].mnc, lab.plmns["operator_b"].mnc_length), ("002", 3))

    def test_override_remaps_nodes_without_touching_topology(self):
        phys = load("multi-bsc.yaml")
        sim = load("multi-bsc.yaml", overrides=["overrides/one-host.yaml"])
        self.assertEqual(phys.instances["bsc-01"].bind, "192.168.50.12")
        self.assertEqual(sim.instances["bsc-01"].bind, "127.0.2.1")
        self.assertTrue(all(n.local for n in sim.nodes.values()))
        self.assertEqual(render.render(phys, "bsc-01").config.split("e1_input")[0].count("bind"), 2)

    def test_placement_named_address(self):
        lab = load("multi-bsc.yaml", "core-bsc")
        self.assertEqual(lab.instances["bsc-02"].bind, "192.168.50.21")

    def test_all_examples_valid(self):
        for f, deps in (("single-node.yaml", [None]), ("multi-plmn.yaml", [None]), ("sdr-b210.yaml", [None]),
                        ("multi-bsc.yaml", ["all-in-one", "split", "core-bsc", "bsc-per-node"])):
            for d in deps:
                for o in ([], ["overrides/one-host.yaml"]):
                    errs = [i for i in validate.validate(load(f, d, o)) if i.level == "error"]
                    self.assertEqual(errs, [], f"{f} {d} {o}")


if __name__ == "__main__":
    unittest.main()
