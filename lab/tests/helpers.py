import copy
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from gsmlab import model, render, validate  # noqa: E402

TOPO = os.path.join(os.path.dirname(HERE), "topologies")


def base():
    """Smallest complete network: one node, one PLMN, MSC + BSC + one BTS."""
    return copy.deepcopy({
        "lab": {"name": "t", "run_dir": "/tmp/gsm-lab-test/{lab}"},
        "nodes": {"n1": {"host": "localhost"}},
        "plmns": {"p": {"mcc": "001", "mnc": "01"}},
        "instances": {
            "stp": {"service": "stp", "node": "n1"},
            "hlr": {"service": "hlr", "node": "n1"},
            "mgw": {"service": "mgw", "node": "n1"},
            "msc": {"service": "msc", "node": "n1", "point_code": "0.23.1"},
            "bsc": {"service": "bsc", "node": "n1", "point_code": "0.23.3"},
            "bts1": {"service": "bts", "variant": "virtual", "node": "n1"},
        },
        "networks": {"net": {
            "plmn": "p", "stp": "stp", "msc": "msc", "hlr": "hlr", "mgw": "mgw",
            "bscs": {"bsc": {"bts": {"bts1": {"lac": 1, "ci": 1, "unit_id": 100, "trx": [{"arfcn": 871}]}}}}}},
    })


def build(raw, deployment=None):
    return model.build(raw, deployment)


def issues(raw, deployment=None):
    return validate.validate(build(raw, deployment))


def codes(raw, level="error", deployment=None):
    return sorted({i.code for i in issues(raw, deployment) if i.level == level})


def cfg(raw, iid, deployment=None):
    return render.render(build(raw, deployment), iid).config


def load(name, deployment=None, overrides=()):
    return model.load(os.path.join(TOPO, name), deployment, [os.path.join(TOPO, o) for o in overrides])
