"""Derived topology: who connects to whom, over which endpoint, and in which order things start.

Everything here is computed from the lab model; nothing is stored. The same link list drives the
validator (reachability, endpoints), the renderer (remote addresses in each .cfg), the start order and
the health checks, so the four can never disagree.
"""

from dataclasses import dataclass

from .services import TIER


@dataclass(frozen=True)
class Link:
    client: str          # instance that opens the connection
    server: str          # instance it connects to
    port: str            # server port name (services.Port.name) or 'unix'/'multicast'
    purpose: str         # human readable: 'A-interface M3UA', 'GSUP', 'MGCP', 'Abis OML', ...
    network: str


def links(lab):
    out = []
    seen = set()

    def add(c, s, port, purpose, net):
        if c and s and (c, s, port) not in seen:
            seen.add((c, s, port))
            out.append(Link(c, s, port, purpose, net))

    for n in lab.networks.values():
        add(n.msc, n.stp, "m3ua", "A-interface signalling (M3UA/SCCP) MSC->STP", n.id)
        add(n.msc, n.hlr, "gsup", "GSUP (subscriber data, auth) MSC->HLR", n.id)
        add(n.msc, n.mgw, "mgcp", "MGCP (MSC media) MSC->MGW", n.id)
        for b in n.bscs:
            add(b.id, n.stp, "m3ua", "A-interface signalling (M3UA/SCCP) BSC->STP", n.id)
            add(b.id, b.mgw, "mgcp", "MGCP (BSC media) BSC->MGW", n.id)
            for c in b.cells:
                add(c.id, b.id, "abis_oml", "Abis/IP OML+RSL BTS->BSC", n.id)
                phys = {t.phy for t in c.trx if t.phy}
                for phy in sorted(phys):
                    add(c.id, phy, "trxd", "TRXC/TRXD (osmo-bts-trx <-> osmo-trx)", n.id)
    for i in lab.instances.values():
        if i.service == "mobile" and i.opts.get("virtphy"):
            add(i.id, i.opts["virtphy"], "unix", "L1CTL unix socket mobile->virtphy", "")
    return out


def bsc_msc_pairs(lab):
    """(network, bsc_link, msc_id) for every A-interface relationship."""
    return [(n, b, n.msc) for n in lab.networks.values() for b in n.bscs]


def ss7_domains(lab):
    """STP id -> list of (instance id, network id) of the MSCs/BSCs whose M3UA ASPs it serves.
    Point codes must be unique inside one domain; separate STPs are separate domains."""
    dom = {}
    for n in lab.networks.values():
        if not n.stp:
            continue
        members = dom.setdefault(n.stp, [])
        for iid in [n.msc] + [b.id for b in n.bscs]:
            if iid and (iid, n.id) not in members:
                members.append((iid, n.id))
    return dom


def start_order(lab, only=None):
    """Instances in dependency order: servers before their clients, ties broken by service tier and
    declaration order. `only` restricts the result (dependencies are NOT added implicitly)."""
    deps = {i: set() for i in lab.instances}
    for l in links(lab):
        if l.client in deps and l.server in deps:
            deps[l.client].add(l.server)
    for i in lab.instances.values():
        if i.service == "mobile" and i.opts.get("virtphy") in deps:
            deps[i.id].add(i.opts["virtphy"])
        if i.service == "virtphy":   # a phone is only useful once cells are up
            deps[i.id] |= {b for b, inst in lab.instances.items() if inst.service == "bts" and inst.node == i.node}
    decl = {k: n for n, k in enumerate(lab.instances)}
    key = lambda k: (TIER.get(lab.instances[k].service, 9), decl[k])
    done, order = set(), []
    pending = sorted(deps, key=key)
    while pending:
        ready = [k for k in pending if deps[k] <= done]
        if not ready:      # cycle: should not happen with the fixed link types; fall back to tier order
            ready = pending[:1]
        k = ready[0]
        order.append(k)
        done.add(k)
        pending.remove(k)
    if only is not None:
        order = [k for k in order if k in only]
    return order, deps
