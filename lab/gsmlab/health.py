"""Health in levels -- a started process is not a working network:

    L1 process   pid alive and still the program gsm-lab started
    L2 control   VTY answers
    L3 service   the instance's links are up, as seen by the instance itself:
                   STP  : one ACTIVE ASP per MSC/BSC of its SS7 domain
                   HLR  : a GSUP connection per MSC using it
                   MSC  : MGCP link to its MGW
                   BSC  : MGCP link; A interface (BSSMAP RESET acknowledged); per cell OML up, RSL up on every TRX
                   mobile: every MS 'MM idle, normal service'
    L4 end-to-end (on request): for each mobile, location update seen by its MSC and an SMS to itself
                   delivered back (same procedure as tests/smoke-core.sh --with-ms)

The VTY regexes are the ones proven in tests/smoke-core.sh against the pinned release set.
"""

import re
import time
from dataclasses import dataclass

from .topology import ss7_domains


@dataclass
class Check:
    instance: str
    level: str      # process | vty | service | e2e
    ok: bool
    detail: str


def _count(regex, text):
    return len(re.findall(regex, text or ""))


def service_checks(rt, iid):
    lab, i = rt.lab, rt.lab.instances[iid]
    ex = rt.ex[i.node]
    vty = lambda *cmds: ex.vty(i.vty.port, list(cmds), ip=i.vty.ip) or ""
    out = []
    add = lambda ok, detail: out.append(Check(iid, "service", ok, detail))

    if i.service == "stp":
        members = [m for m, _ in ss7_domains(lab).get(iid, [])]
        running = [m for m in members if rt.status(m)[0] == "running"]
        n = _count(r"ASP_ACTIVE", vty("show cs7 instance 0 asp"))
        add(n >= len(running), f"{n} ASP(s) active, expected {len(running)} (running MSC/BSC: {', '.join(running) or 'none'})")
    elif i.service == "hlr":
        mscs = [n.msc for n in lab.networks.values() if n.hlr == iid and n.msc and rt.status(n.msc)[0] == "running"]
        n = _count(r"'[^']*MSC[^']*' from", vty("show gsup-connections"))
        add(n >= len(mscs), f"{n} MSC GSUP connection(s), expected {len(mscs)}")
    elif i.service in ("msc", "bsc"):
        txt = vty("show mgw-pool")
        add(bool(re.search(r"MGCP link: +connected,UP", txt)), "MGCP link to MGW " +
            ("up" if re.search(r"MGCP link: +connected,UP", txt) else "DOWN"))
    if i.service == "bsc":
        txt = vty("show mscs")
        ok = "BSSMAP state: CONNECTED" in txt
        add(ok, "A interface: BSSMAP " + ("CONNECTED (RESET acknowledged)" if ok else "not connected"))
        for n, link in lab.bsc_links(iid):
            for c in link.cells:
                t = vty(f"show bts {c.number}")
                oml = "OML Link state: connected" in t
                m = re.search(r"where RSL is up: +(\d+)", t)
                rsl = int(m.group(1)) if m else 0
                add(oml and rsl == len(c.trx),
                    f"bts {c.number} ({c.id}): OML {'up' if oml else 'DOWN'}, RSL up on {rsl}/{len(c.trx)} TRX")
    if i.service == "mobile":
        for k, sim in enumerate(i.opts.get("sims") or [], start=1):
            t = vty(f"show ms {k}")
            ok = "MM idle, normal service" in t
            m = re.search(r"state: (MM [^\n]*)", t)
            add(ok, f"ms {k} ({sim['imsi']}): " + ("registered, normal service" if ok else (m.group(1) if m else "not registered")))
    return out


def check(rt, targets=None, levels=("process", "vty", "service")):
    lab = rt.lab
    res = []
    for iid in targets or list(lab.instances):
        i = lab.instances[iid]
        if not i.enabled:
            continue
        state, pid = rt.status(iid)
        res.append(Check(iid, "process", state == "running",
                         {"running": f"pid {pid}", "stopped": "not started", "dead": f"exited (pid {pid}); see `gsm-lab logs {iid}`",
                          "foreign": f"pid {pid} reused by another program"}.get(state, state)))
        if state != "running":
            continue
        if "vty" in levels and i.vty:
            ok = rt.ex[i.node].vty(i.vty.port, [], ip=i.vty.ip, timeout=2) is not None
            res.append(Check(iid, "vty", ok, f"{i.vty.ip}:{i.vty.port} " + ("answers" if ok else "NOT answering")))
            if not ok:
                continue
        if "service" in levels and i.vty:
            res += service_checks(rt, iid)
    return res


def e2e(rt, timeout=60, out=print):
    """For every mobile SIM: wait for registration, check the MSC VLR saw it, send an SMS to its own
    MSISDN and wait until the phone stored it. Needs msisdn on the SIM."""
    lab = rt.lab
    results = []
    for i in lab.instances.values():
        if i.service != "mobile" or rt.status(i.id)[0] != "running":
            continue
        ex = rt.ex[i.node]
        for k, sim in enumerate(i.opts.get("sims") or [], start=1):
            imsi, msisdn = str(sim["imsi"]), sim.get("msisdn")
            net = next((n for n in lab.networks.values() if lab.plmns.get(n.plmn)
                        and imsi.startswith(lab.plmns[n.plmn].mcc + lab.plmns[n.plmn].mnc)), None)
            name = f"{i.id} ms {k} ({imsi})"
            end = time.time() + timeout
            reg = False
            while time.time() < end and not reg:
                reg = "MM idle, normal service" in (ex.vty(i.vty.port, [f"show ms {k}"], ip=i.vty.ip) or "")
                if not reg:
                    time.sleep(1)
            results.append(Check(i.id, "e2e", reg, f"{name}: " + ("registered" if reg else f"not registered after {timeout}s")))
            out(("[ OK ] " if reg else "[FAIL] ") + results[-1].detail)
            if not reg or not net or not msisdn:
                if reg and not msisdn:
                    out(f"[SKIP] {name}: no msisdn on the SIM, SMS loopback skipped")
                continue
            msc = lab.instances[net.msc]
            mex = rt.ex[msc.node]
            vlr = imsi in (mex.vty(msc.vty.port, ["show subscriber cache"], ip=msc.vty.ip) or "")
            results.append(Check(i.id, "e2e", vlr, f"{name}: {'in' if vlr else 'NOT in'} the VLR of {msc.id}"))
            out(("[ OK ] " if vlr else "[FAIL] ") + results[-1].detail)
            text = f"gsm-lab-{int(time.time())}-{k}"
            ex.vty(i.vty.port, [f"sms {k} {msisdn} {text}"], ip=i.vty.ip)
            smsfile = f"{lab.instance_dir(i)}/.osmocom/bb/sms.txt"
            got = False
            end = time.time() + 30
            while time.time() < end and not got:
                rc, _, _ = ex.run(f"grep -q {text} {smsfile} 2>/dev/null")
                got = rc == 0
                if not got:
                    time.sleep(1)
            results.append(Check(i.id, "e2e", got, f"{name}: SMS to own MSISDN {msisdn} " +
                                 ("delivered (MO via MSC, MT back to the phone)" if got else "NOT delivered within 30s")))
            out(("[ OK ] " if got else "[FAIL] ") + results[-1].detail)
    if not results:
        out("[SKIP] no running mobile instance: end-to-end test needs virtphy + mobile (or a real phone)")
    return results
