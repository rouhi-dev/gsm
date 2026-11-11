"""Static validation of a lab model. No network access, no filesystem writes.

Every finding has a stable code, the config path it refers to, and (where possible) a hint naming the
fix. Severity:
    error  the lab cannot work as described (or would silently do something else); start refuses
    warn   works, but relies on special configuration, is unverified, or is likely a mistake
    info   noteworthy facts (shared resources, ...)

Capability rules (what Osmocom can actually do) are checked here, not hidden in the renderer; the
evidence for each is in services.py and docs/LAB.md "What Osmocom supports".
"""

import ipaddress
import os
import re
from collections import defaultdict
from dataclasses import dataclass

from .services import SERVICES
from .topology import links, ss7_domains


@dataclass
class Issue:
    level: str
    code: str
    path: str
    message: str
    hint: str = ""

    def __str__(self):
        s = f"[{self.level.upper():5}] {self.code}: {self.path}: {self.message}"
        return s + (f"\n        -> {self.hint}" if self.hint else "")


BAND_ARFCN = {  # 3GPP TS 45.005 ARFCN ranges, as accepted by osmo-bsc
    "GSM450": [(259, 293)], "GSM480": [(306, 340)], "GSM850": [(128, 251)],
    "GSM900": [(0, 124), (955, 1023)], "DCS1800": [(512, 885)], "PCS1900": [(512, 810)],
}


def parse_point_code(pc):
    """Osmocom default point-code format 3.8.3 ('0.23.1'), or a plain integer (14 bit). -> int or None"""
    if isinstance(pc, int) and not isinstance(pc, bool):
        return pc if 0 <= pc < 1 << 14 else None
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(pc))
    if not m:
        return None
    a, b, c = map(int, m.groups())
    if a > 7 or b > 255 or c > 7:
        return None
    return a << 11 | b << 3 | c


def _ip(s):
    try:
        return ipaddress.ip_address(s)
    except (ValueError, TypeError):
        return None


def is_loopback(s):
    a = _ip(s)
    return bool(a and a.is_loopback)


def validate(lab, check_files=True):
    issues = []
    E = lambda code, path, msg, hint="": issues.append(Issue("error", code, path, msg, hint))
    W = lambda code, path, msg, hint="": issues.append(Issue("warn", code, path, msg, hint))
    I = lambda code, path, msg, hint="": issues.append(Issue("info", code, path, msg, hint))

    for path, msg in lab.load_errors:
        E("SCHEMA", path or "<top>", msg)
    inst = lab.instances

    # ------------------------------------------------------------ nodes
    for n in lab.nodes.values():
        p = f"nodes.{n.id}"
        if not n.local and not n.address:
            E("NODE-ADDR", p, "remote node has no 'address'",
              "set the lab-facing IP other nodes use to reach services on it")
        if not n.local and not n.ssh_user:
            W("NODE-SSH", p, "no ssh.user; the SSH default (your login name / ~/.ssh/config) will be used")
        if n.address and not _ip(n.address):
            E("NODE-ADDR", f"{p}.address", f"'{n.address}' is not an IP address")
        if not os.path.isabs(n.run_dir):
            E("NODE-PATH", f"{p}.run_dir", f"must be absolute (or start with ~), got '{n.run_dir}'")

    # ------------------------------------------------------------ plmns
    seen_plmn = {}
    for pl in lab.plmns.values():
        p = f"plmns.{pl.id}"
        if not re.fullmatch(r"\d{3}", pl.mcc) or pl.mcc == "000":
            E("PLMN-MCC", f"{p}.mcc", f"MCC must be 3 digits (001-999), got '{pl.mcc}'")
        if not re.fullmatch(r"\d{2,3}", pl.mnc):
            E("PLMN-MNC", f"{p}.mnc", f"MNC must be 2 or 3 digits, got '{pl.mnc}'")
        elif pl.mnc_length != len(pl.mnc):
            E("PLMN-MNC", f"{p}.mnc_length",
              f"mnc_length {pl.mnc_length} does not match MNC '{pl.mnc}' ({len(pl.mnc)} digits)",
              "a 3-digit MNC with a leading zero is written as \"001\", a 2-digit one as \"01\"; they are different PLMNs")
        key = (pl.mcc, pl.mnc)
        if key in seen_plmn:
            E("PLMN-DUP", p, f"MCC/MNC {pl.mcc}/{pl.mnc} already used by plmn '{seen_plmn[key]}'")
        seen_plmn[key] = pl.id

    # ------------------------------------------------------------ instances
    for i in inst.values():
        p = f"instances.{i.id}"
        if i.node is None:
            E("INST-NODE", f"{p}.node", "not placed on any node",
              "set instances.<id>.node or place it in the selected deployment")
            continue
        if i.node not in lab.nodes:
            E("INST-NODE", f"{p}.node", f"unknown node '{i.node}' (have: {', '.join(lab.nodes)})")
            continue
        node = lab.nodes[i.node]
        for ep_name, ep in [("vty", i.vty), ("ctrl", i.ctrl)] + list(i.ports.items()):
            if ep is None:
                continue
            if ep.ip is None or not _ip(ep.ip):
                E("INST-ADDR", f"{p}.{ep_name}", f"invalid or missing address '{ep.ip}'")
            elif not is_loopback(ep.ip) and ep.ip != "0.0.0.0" and ep.ip != node.address \
                    and ep.ip not in node.extra_addresses:
                W("INST-ADDR", f"{p}.{ep_name}",
                  f"{ep.ip} is neither the node address ({node.address}) nor listed in nodes.{node.id}.addresses",
                  "list it under the node's 'addresses' so `gsm-lab doctor` checks it exists there")
            if not 0 < (ep.port or 0) < 65536:
                E("INST-PORT", f"{p}.{ep_name}", f"port {ep.port} out of range")
        svc = SERVICES[i.service]
        for port in svc.ports:
            if port.fixed and i.ports[port.name].port != port.default:
                E("PORT-FIXED", f"{p}.ports.{port.name}",
                  f"{svc.binary} cannot change this port (fixed at {port.default}: {port.note})",
                  "give the instance a different IP address instead (e.g. bind: 127.0.0.2)")
        if i.service in ("msc", "bsc"):
            pc = i.opts.get("point_code")
            if pc is None:
                E("SS7-PC", f"{p}.point_code", "missing point code")
            elif parse_point_code(pc) is None:
                E("SS7-PC", f"{p}.point_code", f"invalid point code {pc!r}",
                  "Osmocom default format is 3.8.3, e.g. 0.23.1")
        if i.service == "stp" and i.opts.get("point_code") is not None and parse_point_code(i.opts["point_code"]) is None:
            E("SS7-PC", f"{p}.point_code", f"invalid point code {i.opts['point_code']!r}")
        if i.service == "mgw":
            rng = i.opts.get("rtp_range", [4002, 16001])
            if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, int) for x in rng) and rng[0] < rng[1]):
                E("MGW-RTP", f"{p}.rtp_range", f"must be [low, high], got {rng!r}")
        if i.service == "mobile":
            vp = i.opts.get("virtphy")
            if vp not in inst or inst[vp].service != "virtphy":
                E("MS-PHY", f"{p}.virtphy", f"must name a virtphy instance, got {vp!r}")
            elif inst[vp].node != i.node:
                E("MS-PHY", f"{p}.virtphy", "mobile and its virtphy talk over a unix socket: they must be on the same node")
            for k, sim in enumerate(i.opts.get("sims") or []):
                if not re.fullmatch(r"\d{15}", str(sim.get("imsi", ""))):
                    E("MS-SIM", f"{p}.sims[{k}].imsi", f"IMSI must be 15 digits, got {sim.get('imsi')!r}")
                if sim.get("ki") and not re.fullmatch(r"[0-9a-fA-F]{32}", str(sim["ki"])):
                    E("MS-SIM", f"{p}.sims[{k}].ki", "Ki must be 32 hex digits")
            if not i.opts.get("sims"):
                E("MS-SIM", f"{p}.sims", "a mobile needs at least one SIM (imsi, ki)")
        if i.config_file:
            W("INST-RAWCFG", f"{p}.config_file",
              "hand-written config: gsm-lab starts it as-is and cannot check that its addresses, point codes and "
              "ports match the topology")
            if check_files and not os.path.isabs(i.config_file):
                E("INST-RAWCFG", f"{p}.config_file", "must be an absolute path (it is read on the node)")

    # ------------------------------------------------------------ networks: references and roles
    def ref(path, iid, service, required=True):
        if iid is None:
            if required:
                E("REF-MISSING", path, f"no {service} given")
            return False
        if iid not in inst:
            E("REF-UNKNOWN", path, f"unknown instance '{iid}'")
            return False
        if inst[iid].service != service:
            E("REF-TYPE", path, f"'{iid}' is a {inst[iid].service}, expected a {service}")
            return False
        return True

    msc_nets, bsc_nets, bts_cells, trx_users = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    hlr_plmns, mgw_nets, stp_nets = defaultdict(set), defaultdict(set), defaultdict(set)
    lac_ci = {}
    for n in lab.networks.values():
        p = f"networks.{n.id}"
        if n.plmn not in lab.plmns:
            E("REF-PLMN", f"{p}.plmn", f"unknown PLMN {n.plmn!r} (have: {', '.join(lab.plmns) or 'none'})")
        if n.routing not in ("dynamic", "static"):
            E("SS7-ROUTING", f"{p}.routing", f"must be 'dynamic' or 'static', got {n.routing!r}")
        if n.authentication not in ("optional", "required"):
            E("NET-AUTH", f"{p}.authentication", "must be 'optional' or 'required'")
        has_msc = ref(f"{p}.msc", n.msc, "msc", required=bool(n.bscs))
        ref(f"{p}.stp", n.stp, "stp", required=has_msc or bool(n.bscs))
        if has_msc:
            ref(f"{p}.hlr", n.hlr, "hlr")
            ref(f"{p}.mgw", n.mgw, "mgw")
            msc_nets[n.msc].append(n.id)
        if n.hlr in inst:
            hlr_plmns[n.hlr].add(n.plmn)
        if n.mgw in inst:
            mgw_nets[n.mgw].add(n.id)
        if n.stp in inst:
            stp_nets[n.stp].add(n.id)
        if not n.bscs:
            W("NET-NO-BSC", f"{p}.bscs", "network has no BSC (core only)")
        for b in n.bscs:
            bp = f"{p}.bscs.{b.id}"
            if not ref(bp, b.id, "bsc"):
                continue
            bsc_nets[b.id].append(n.id)
            ref(f"{bp}.mgw", b.mgw, "mgw")
            if b.mgw in inst:
                mgw_nets[b.mgw].add(n.id)
            if b.ran_plmn:
                if b.ran_plmn not in lab.plmns:
                    E("REF-PLMN", f"{bp}.ran_plmn", f"unknown PLMN {b.ran_plmn!r}")
                elif b.ran_plmn != n.plmn:
                    W("PLMN-REWRITE", f"{bp}.ran_plmn",
                      f"BSC broadcasts {b.ran_plmn} but presents {n.plmn} to the MSC "
                      "(osmo-bsc 'msc N / core-mobile-country-code / core-mobile-network-code'): special "
                      "configuration; the cell identities seen by the MSC are rewritten; not verified end-to-end")
            nums, units = {}, {}
            if not b.cells:
                W("BSC-NO-BTS", f"{bp}.bts", "BSC controls no BTS")
            for c in b.cells:
                cp = f"{bp}.bts.{c.id}"
                if not ref(cp, c.id, "bts"):
                    continue
                bts_cells[c.id].append(cp)
                if c.number in nums:
                    E("BTS-NUM", f"{cp}.number", f"bts number {c.number} already used by {nums[c.number]}")
                nums[c.number] = c.id
                if c.unit_id is None:
                    E("BTS-UNIT", f"{cp}.unit_id", "missing IPA unit id (how the BSC recognises this BTS)")
                elif c.unit_id in units:
                    E("BTS-UNIT", f"{cp}.unit_id", f"unit id {c.unit_id} already used by {units[c.unit_id]} on this BSC")
                else:
                    units[c.unit_id] = c.id
                for f, rng in (("lac", (1, 65533)), ("ci", (0, 65535)), ("bsic", (0, 63))):
                    v = getattr(c, f)
                    if v is None:
                        E("CELL-ID", f"{cp}.{f}", f"missing {f}")
                    elif not rng[0] <= v <= rng[1]:
                        E("CELL-ID", f"{cp}.{f}", f"{f} {v} outside {rng[0]}..{rng[1]}")
                if c.lac is not None and c.ci is not None:
                    key = (b.ran_plmn or n.plmn, c.lac, c.ci)
                    if key in lac_ci:
                        E("CELL-DUP", cp, f"LAC/CI {c.lac}/{c.ci} already used in PLMN {key[0]} by {lac_ci[key]}")
                    lac_ci[key] = c.id
                if c.band not in BAND_ARFCN:
                    E("CELL-BAND", f"{cp}.band", f"unknown band {c.band!r} (one of {', '.join(BAND_ARFCN)})")
                variant = inst[c.id].variant
                phys, chans = set(), set()
                if not c.trx:
                    E("TRX-NONE", f"{cp}.trx", "a BTS needs at least one TRX")
                for t in c.trx:
                    tp = f"{cp}.trx[{t.index}]"
                    if t.arfcn is None:
                        E("TRX-ARFCN", f"{tp}.arfcn", "missing ARFCN")
                    elif c.band in BAND_ARFCN and not any(lo <= t.arfcn <= hi for lo, hi in BAND_ARFCN[c.band]):
                        E("TRX-ARFCN", f"{tp}.arfcn", f"ARFCN {t.arfcn} is not in band {c.band}")
                    if len(t.timeslots) != 8:
                        E("TRX-TS", f"{tp}.timeslots", "need exactly 8 timeslot configurations")
                    if variant == "trx":
                        if ref(f"{tp}.phy", t.phy, "trx"):
                            trx_users[t.phy].append(c.id)
                            phys.add(t.phy)
                        if t.channel in chans:
                            E("TRX-CHAN", f"{tp}.channel", f"channel {t.channel} used twice")
                        chans.add(t.channel)
                    elif t.phy:
                        E("TRX-PHY", f"{tp}.phy", "only osmo-bts-trx (variant: trx) uses an osmo-trx instance")
                if len(phys) > 1:
                    E("TRX-PHY", f"{cp}.trx", "all TRXs of one BTS must use the same osmo-trx instance (as its channels)")
                if variant == "trx" and c.trx and c.trx[0].index == 0 and len(c.trx) > 1 and c.trx[0].timeslots[0] != "CCCH+SDCCH4" \
                        and not c.trx[0].timeslots[0].startswith("CCCH"):
                    W("TRX-TS", f"{cp}.trx[0].timeslots", "TRX 0 timeslot 0 normally carries the BCCH/CCCH")

    # ------------------------------------------------------------ capability rules (multi-PLMN, sharing)
    for m, nets in msc_nets.items():
        if len(nets) > 1:
            plmns = {lab.networks[x].plmn for x in nets}
            if len(plmns) > 1:
                E("CAP-MSC-PLMN", f"instances.{m}",
                  f"one OsmoMSC serves exactly one PLMN, but networks {', '.join(nets)} give it {', '.join(sorted(plmns))}",
                  "run one MSC instance per PLMN (they can share a node)")
            else:
                E("CAP-MSC-NETS", f"instances.{m}", f"MSC appears in several networks ({', '.join(nets)})",
                  "put all its BSCs under one network: networks.<id>.bscs")
    for b, nets in bsc_nets.items():
        if len(nets) > 1:
            E("CAP-BSC-NETS", f"instances.{b}", f"BSC appears in several networks ({', '.join(nets)})",
              "an OsmoBSC broadcasts one PLMN; connecting it to several MSCs is MSC pooling (NRI), "
              "which gsm-lab does not model yet -- see ~/osmocom/msc-pool-lab")
    for h, plmns in hlr_plmns.items():
        if len(plmns) > 1:
            W("CAP-HLR-SHARED", f"instances.{h}",
              f"one HLR serves PLMNs {', '.join(sorted(plmns))}: GSUP works for any MSC, but home/roaming "
              "separation of subscribers is not enforced and not verified", "use one HLR per PLMN for independent networks")
    for m, nets in mgw_nets.items():
        if len(nets) > 1:
            I("SHARED-MGW", f"instances.{m}", f"MGW shared by networks {', '.join(sorted(nets))}; check its RTP range and endpoint count")
    for s, nets in stp_nets.items():
        if len(nets) > 1:
            I("SHARED-STP", f"instances.{s}",
              f"STP shared by networks {', '.join(sorted(nets))}: one SS7 domain, point codes must be unique across them")
    for c, uses in bts_cells.items():
        if len(uses) > 1:
            E("BTS-DUP", f"instances.{c}", f"BTS used as several cells: {', '.join(uses)}")
    for t, users in trx_users.items():
        if len(set(users)) > 1:
            E("TRX-SHARED", f"instances.{t}", f"osmo-trx instance used by several BTSs: {', '.join(sorted(set(users)))}")

    # ------------------------------------------------------------ signalling (point codes per STP domain)
    for stp, members in ss7_domains(lab).items():
        seen = {}
        if stp in inst and inst[stp].opts.get("point_code") is not None:
            pc = parse_point_code(inst[stp].opts["point_code"])
            if pc is not None:
                seen[pc] = stp
        routing = {lab.networks[nid].routing for _, nid in members}
        for iid, nid in members:
            if iid not in inst:
                continue
            pc = parse_point_code(inst[iid].opts.get("point_code"))
            if pc is None:
                continue
            if pc in seen and seen[pc] != iid:
                E("SS7-PC-DUP", f"instances.{iid}.point_code",
                  f"point code {inst[iid].opts['point_code']} already used by '{seen[pc]}' in the SS7 domain of STP '{stp}'")
            seen[pc] = iid
            if "static" in routing and inst[iid].opts.get("m3ua_local_port") is None:
                E("SS7-STATIC", f"instances.{iid}.m3ua_local_port",
                  "static M3UA routing needs a fixed local port on each ASP so the STP can tell them apart",
                  "set m3ua_local_port (e.g. 2911) or use routing: dynamic")
        if len(routing) > 1:
            E("SS7-ROUTING", f"instances.{stp}", "networks sharing this STP mix static and dynamic routing")

    # ------------------------------------------------------------ links: reachability of server endpoints
    for l in links(lab):
        if l.client not in inst or l.server not in inst:
            continue
        ci, si = inst[l.client], inst[l.server]
        if ci.node not in lab.nodes or si.node not in lab.nodes or l.port in ("unix", "multicast"):
            continue
        ep = si.ports.get(l.port)
        if ep is None:
            continue
        remote = lab.nodes[ci.node].machine != lab.nodes[si.node].machine
        if remote and is_loopback(ep.ip):
            E("LINK-LOOPBACK", f"instances.{l.server}.ports.{l.port}",
              f"{l.client} on {ci.node} must reach {l.server} on {si.node} ({l.purpose}), but it listens on {ep.ip}",
              f"bind it to an address reachable from {ci.node}, e.g. nodes.{si.node}.address")
        # only links whose client socket is bound to the instance address (M3UA / MGCP 'local-ip');
        # Abis (osmo-bts) and GSUP (osmo-msc) clients let the kernel choose the source address
        if remote and ci.bind and is_loopback(ci.bind) and l.port in ("m3ua", "mgcp"):
            E("LINK-LOOPBACK", f"instances.{l.client}.bind",
              f"{l.client} binds its sockets to {ci.bind} but talks to {si.node} ({l.purpose})")
    for i in inst.values():
        if i.service == "bts" and i.variant == "trx":
            for n, b, c in lab.cells():
                if c.id == i.id:
                    for phy in {t.phy for t in c.trx if t.phy in inst}:
                        if inst[phy].node in lab.nodes and i.node in lab.nodes and \
                                lab.nodes[inst[phy].node].machine != lab.nodes[i.node].machine:
                            W("TRX-REMOTE", f"instances.{phy}.node",
                              f"osmo-trx {phy} runs on {inst[phy].node}, its BTS on {i.node}: TRXD over the network "
                              "is supported but timing-sensitive")

    # ------------------------------------------------------------ unused instances
    used = {x for l in links(lab) for x in (l.client, l.server)} | set(bts_cells)
    for i in inst.values():
        if i.id not in used and i.service not in ("virtphy",):
            W("INST-UNUSED", f"instances.{i.id}", "not referenced by any network")

    # ------------------------------------------------------------ collisions on each node
    issues += collisions(lab)
    return issues


def _listeners(lab):
    """Yield (instance, label, node, proto, ip, lo_port, hi_port) for every socket an instance binds."""
    for i in lab.instances.values():
        if i.node is None or not i.enabled or i.config_file:
            continue
        if i.service != "virtphy":
            yield i, "vty", i.node, "tcp", i.vty.ip, i.vty.port, i.vty.port
        if i.ctrl:
            yield i, "ctrl", i.node, "tcp", i.ctrl.ip, i.ctrl.port, i.ctrl.port
        for name, ep in i.ports.items():
            if name == "abis_oml":
                yield i, "abis (OML+RSL)", i.node, "tcp", ep.ip, ep.port, i.ports["abis_rsl"].port
                continue
            if name == "abis_rsl":
                continue
            if name == "trxd":
                nch = max(1, len([t for _, _, c in lab.cells() for t in c.trx if t.phy == i.id]))
                yield i, "trx base ports", i.node, "udp", ep.ip, ep.port, ep.port + 2 * nch + 2
                continue
            if name == "m3ua":
                proto = i.opts.get("transport", "tcp")
                yield i, name, i.node, proto, ep.ip, ep.port, ep.port
                continue
            yield i, name, i.node, ep.proto, ep.ip, ep.port, ep.port
        if i.service == "mgw":
            lo, hi = (i.opts.get("rtp_range") or [4002, 16001])[:2]
            yield i, "rtp range", i.node, "udp", i.opts.get("rtp_bind", i.ports["mgcp"].ip), lo, hi
        if i.service in ("msc", "bsc") and i.opts.get("m3ua_local_port"):
            p = i.opts["m3ua_local_port"]
            yield i, "m3ua client port", i.node, i.opts.get("transport", "tcp"), i.bind, p, p
        if i.service == "bts" and i.variant == "trx":
            base = (i.opts.get("trx_options") or {}).get("local_base_port", 5800)
            nch = max(1, len(lab.cell(i.id)[2].trx) if lab.cell(i.id) else 1)
            yield i, "trx client base ports", i.node, "udp", i.bind, base, base + 2 * nch + 2


def collisions(lab):
    issues = []
    by_key = defaultdict(list)
    for item in _listeners(lab):
        i, label, node, proto, ip, lo, hi = item
        machine = lab.nodes[node].machine if node in lab.nodes else node
        by_key[(machine, proto)].append(item)     # SCTP, TCP and UDP ports are separate namespaces
    for (machine, proto), items in by_key.items():
        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                ia, la, _, _, ipa, loa, hia = items[a]
                ib, lb, _, _, ipb, lob, hib = items[b]
                same_ip = ipa == ipb or "0.0.0.0" in (ipa, ipb)
                if same_ip and loa <= hib and lob <= hia:
                    rng = lambda lo, hi: str(lo) if lo == hi else f"{lo}-{hi}"
                    issues.append(Issue(
                        "error", "PORT-CONFLICT", f"instances.{ib.id}",
                        f"{lb} {ipb}:{rng(lob, hib)}/{proto} overlaps {ia.id} {la} {ipa}:{rng(loa, hia)} on {machine}",
                        "give one of them another port, or another IP (127.0.0.x works on loopback); "
                        "gsm-lab never reassigns ports by itself"))
    return issues


def summarize(issues):
    return {lvl: sum(1 for i in issues if i.level == lvl) for lvl in ("error", "warn", "info")}
