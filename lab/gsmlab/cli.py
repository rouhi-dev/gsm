"""gsm-lab command line."""

import argparse
import os
import shlex
import subprocess
import sys

from . import health, model, nodes, render, repo, topology, validate
from .runtime import Runtime

DEFAULT_FILE = os.environ.get("GSM_LAB_FILE", os.path.join(model.REPO_ROOT, "lab", "lab.yaml"))


def load(args, strict=True):
    try:
        lab = model.load(args.file, args.deployment, args.override or ())
    except model.LabError as e:
        sys.exit(f"[FAIL] {e}")
    issues = validate.validate(lab)
    errs = [i for i in issues if i.level == "error"]
    if strict and errs:
        for i in errs:
            print(i)
        sys.exit(f"[FAIL] {len(errs)} configuration error(s): fix them first (`gsm-lab validate` lists all findings)")
    return lab


def resolve(lab, ids):
    for i in ids or []:
        if i not in lab.instances:
            sys.exit(f"[FAIL] unknown instance '{i}' (have: {', '.join(lab.instances)})")
    return ids or None


# ------------------------------------------------------------ commands
def c_validate(args):
    try:
        lab = model.load(args.file, args.deployment, args.override or ())
    except model.LabError as e:
        print(f"[FAIL] {e}")
        return 2
    issues = validate.validate(lab)
    for i in issues:
        if i.level != "info" or args.verbose:
            print(i)
    s = validate.summarize(issues)
    print(f"\n{lab.name}: {len(lab.nodes)} node(s), {len(lab.plmns)} PLMN(s), {len(lab.networks)} network(s), "
          f"{len(lab.instances)} instance(s)" + (f", deployment '{lab.deployment}'" if lab.deployment else ""))
    print(f"Result: {s['error']} error(s), {s['warn']} warning(s), {s['info']} note(s)")
    return 1 if s["error"] or (args.strict and s["warn"]) else 0


def c_nodes(args):
    lab = load(args, strict=False)
    for n in lab.nodes.values():
        where = "local" if n.local else f"ssh {n.ssh_user + '@' if n.ssh_user else ''}{n.host}:{n.ssh_port}"
        here = [i.id for i in lab.instances.values() if i.node == n.id]
        print(f"{n.id:10} {where:28} address={n.address}  prefix={n.prefix}")
        print(f"{'':10} instances: {', '.join(here) or '-'}")
        if args.status:
            ok, info = nodes.ping_node(nodes.NodeExec(n))
            print(f"{'':10} {'reachable' if ok else 'UNREACHABLE'} ({info})")
    return 0


def c_topology(args):
    lab = load(args, strict=False)
    inst = lab.instances
    where = lambda i: f"@{inst[i].node}" if i in inst else "@?"
    for n in lab.networks.values():
        pl = lab.plmns.get(n.plmn)
        print(f"network {n.id}  PLMN {n.plmn}" + (f" ({pl.mcc}/{pl.mnc} {pl.name})" if pl else "") +
              f"  routing={n.routing}")
        print(f"  STP {n.stp}{where(n.stp)}   MSC {n.msc}{where(n.msc)} pc={inst[n.msc].opts.get('point_code') if n.msc in inst else '?'}"
              f"   HLR {n.hlr}{where(n.hlr)}   MGW {n.mgw}{where(n.mgw)}")
        for b in n.bscs:
            pc = inst[b.id].opts.get("point_code") if b.id in inst else "?"
            extra = f" broadcasts {b.ran_plmn}" if b.ran_plmn else ""
            print(f"  └─ BSC {b.id}{where(b.id)} pc={pc} mgw={b.mgw}{extra}")
            for c in b.cells:
                v = inst[c.id].variant if c.id in inst else "?"
                print(f"      └─ BTS {c.number} {c.id}{where(c.id)} ({v}) LAC {c.lac} CI {c.ci} BSIC {c.bsic} unit-id {c.unit_id} {c.band}")
                for t in c.trx:
                    phy = f" via {t.phy}{where(t.phy)} chan {t.channel}" if t.phy else ""
                    print(f"          └─ TRX {t.index} ARFCN {t.arfcn}{phy}")
    if args.links:
        print("\nlinks (client -> server endpoint):")
        for l in topology.links(lab):
            s = inst.get(l.server)
            ep = s.ports.get(l.port) if s else None
            dest = f"{ep.ip}:{ep.port}" if ep else l.port
            print(f"  {l.client:12} -> {l.server:12} {dest:22} {l.purpose}")
    others = [i for i in inst.values() if i.service in ("virtphy", "mobile")]
    if others:
        print("\nphones: " + ", ".join(f"{i.id}{where(i.id)}" for i in others))
    return 0


def c_services(args):
    lab = load(args, strict=False)
    rt = Runtime(lab) if args.status else None
    print(f"{'INSTANCE':12} {'SERVICE':8} {'NODE':9} {'VTY':20} {'LISTENS':40}" + (" STATE" if rt else ""))
    for i in lab.instances.values():
        listens = ", ".join(f"{k}={e.ip}:{e.port}" for k, e in i.ports.items() if k not in ("abis_rsl", "mgcp_client"))
        vty = f"{i.vty.ip}:{i.vty.port}" if i.vty else "-"
        line = f"{i.id:12} {i.service + ('/' + i.variant if i.variant else ''):8} {str(i.node):9} {vty:20} {listens:40}"
        if rt:
            st, pid = rt.status(i.id)
            line += f" {st}" + (f" ({pid})" if pid else "")
        print(line)
    return 0


def c_render(args):
    lab = load(args)
    ids = resolve(lab, args.instances) or list(lab.instances)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
    for iid in ids:
        l = render.render(lab, iid)
        if args.out:
            if l.config:
                with open(os.path.join(args.out, f"{iid}.cfg"), "w") as f:
                    f.write(l.config)
            with open(os.path.join(args.out, f"{iid}.cmd"), "w") as f:
                f.write(" ".join(f"{k}={shlex.quote(v)}" for k, v in l.env.items()) + " " +
                        " ".join(shlex.quote(a) for a in l.argv) + "\n")
            print(f"{iid}: {args.out}/{iid}.cfg")
        else:
            print(f"### {iid} on {l.node}: cd {l.workdir} && {' '.join(l.argv)}")
            print(l.config or "(no config file)")
    return 0


def c_start(args):
    lab = load(args)
    rt = Runtime(lab, backend=args.backend)
    res, failed = rt.start(resolve(lab, args.instances), dry_run=args.dry_run, with_deps=not args.no_deps)
    if args.dry_run:
        print("\n(dry run: nothing was written or started)")
        return 0
    if failed:
        print(f"\nResult: {len(failed)} instance(s) failed or blocked: {', '.join(sorted(failed))}")
        return 1
    print("\nAll processes up and their VTYs answer. Links come up over the next seconds: `gsm-lab health`.")
    return 0


def c_stop(args):
    lab = load(args, strict=False)
    Runtime(lab, backend=args.backend).stop(resolve(lab, args.instances), with_dependents=not args.no_deps)
    return 0


def c_restart(args):
    lab = load(args)
    rt = Runtime(lab, backend=args.backend)
    ids = resolve(lab, args.instances)
    rt.stop(ids, with_dependents=False)
    _, failed = rt.start(ids, with_deps=False)
    return 1 if failed else 0


def _print_checks(checks):
    bad = 0
    for c in checks:
        bad += not c.ok
        print(f"{'[ OK ]' if c.ok else '[FAIL]'} {c.instance:12} {c.level:8} {c.detail}")
    return bad


def c_status(args):
    lab = load(args, strict=False)
    rt = Runtime(lab)
    bad = _print_checks(health.check(rt, resolve(lab, args.instances), levels=("process", "vty")))
    return 1 if bad else 0


def c_health(args):
    lab = load(args, strict=False)
    rt = Runtime(lab)
    bad = _print_checks(health.check(rt, resolve(lab, args.instances)))
    if args.e2e:
        print()
        bad += sum(not c.ok for c in health.e2e(rt, timeout=args.timeout))
    print(f"\nResult: {'OK' if not bad else f'{bad} check(s) failed'}")
    return 1 if bad else 0


def c_logs(args):
    lab = load(args, strict=False)
    resolve(lab, [args.instance])
    rt = Runtime(lab)
    if args.follow:
        l = render.render(lab, args.instance)
        ex = rt.ex[l.node]
        return subprocess.call(ex._cmd(f"tail -n {args.lines} -F {shlex.quote(l.workdir + '/out.log')}"))
    print(rt.log_tail(args.instance, args.lines), end="")
    return 0


def c_doctor(args):
    lab = load(args, strict=False)
    issues = validate.validate(lab)
    errs = [i for i in issues if i.level == "error"]
    print(f"== Configuration: {len(errs)} error(s), {sum(i.level == 'warn' for i in issues)} warning(s)")
    for i in errs:
        print(i)
    rt = Runtime(lab, backend=args.backend)
    cache, fails = {}, len(errs)
    print("\n== Nodes")
    for n in lab.nodes.values():
        ok, info = nodes.ping_node(rt.ex[n.id])
        print(f"{'[ OK ]' if ok else '[FAIL]'} {n.id}: {'reachable' if ok else 'unreachable'} ({info})")
        fails += not ok
        if ok:
            pr = rt.ex[n.id].probe()
            print(f"       {pr['uname']}; kernel SCTP: {'yes' if pr['sctp'] else 'NO'}; "
                  + ", ".join(f"{t}: {'yes' if v else 'MISSING'}" for t, v in pr["tools"].items()))
    print("\n== Instances (binary, addresses, free ports) -- running instances are skipped")
    for iid in topology.start_order(lab)[0]:
        if rt.status(iid)[0] == "running":
            print(f"[ OK ] {iid}: running")
            continue
        probs = rt.preflight(iid, cache)
        fails += bool(probs)
        print(f"{'[ OK ]' if not probs else '[FAIL]'} {iid} on {lab.instances[iid].node}")
        for p in probs:
            print(f"       -> {p}")
    print("\n== Cross-node reachability of running servers")
    for l in topology.links(lab):
        c, s = lab.instances.get(l.client), lab.instances.get(l.server)
        if not c or not s or c.node == s.node or l.port not in ("m3ua", "gsup", "abis_oml"):
            continue
        if rt.status(l.server)[0] != "running":
            continue
        ep = s.ports[l.port]
        ok = rt.ex[c.node].tcp_reachable(ep.ip, ep.port)
        fails += not ok
        print(f"{'[ OK ]' if ok else '[FAIL]'} {c.node} -> {ep.ip}:{ep.port} ({l.purpose})")
    print(f"\nResult: {'OK' if not fails else f'{fails} problem(s)'}")
    return 1 if fails else 0


def c_tmuxinator(args):
    """Export a tmuxinator project whose panes run `gsm-lab exec <instance>`: the lab file stays the only
    source of truth, tmuxinator is just a viewer/launcher."""
    lab = load(args)
    me = os.path.join(model.REPO_ROOT, "gsm-lab")
    base = f"{shlex.quote(me)} -f {shlex.quote(os.path.abspath(args.file))}" + \
           (f" --deployment {shlex.quote(lab.deployment)}" if lab.deployment else "")
    groups = {"core": ("stp", "hlr", "mgw", "msc"), "ran": ("bsc", "trx", "bts"), "ms": ("virtphy", "mobile")}
    order = topology.start_order(lab)[0]
    y = [f"# GENERATED by `gsm-lab tmuxinator` from {os.path.abspath(args.file)} -- do not edit, re-export instead",
         f"name: gsm-lab-{lab.name}", f"root: {model.REPO_ROOT}", "startup_window: core", "windows:"]
    for win, svcs in groups.items():
        panes = []
        for iid in order:
            i = lab.instances[iid]
            if i.service not in svcs or not i.enabled:
                continue
            n = lab.nodes[i.node]
            cmd = f"{base} exec {iid}"
            if not n.local:
                cmd = f"ssh -t {(n.ssh_user + '@') if n.ssh_user else ''}{n.host} {shlex.quote(cmd)}"
            panes.append(f"        - sleep {0 if win == 'core' else 3 if win == 'ran' else 8}; {cmd}")
        if panes:
            y += [f"  - {win}:", "      layout: tiled", "      panes:"] + panes
    y += ["  - health:", "      panes:",
          f"        - sleep 5; {base} provision; watch -n 3 {shlex.quote(base + ' status')}"]
    text = "\n".join(y) + "\n"
    if args.write:
        path = os.path.expanduser(f"~/.config/tmuxinator/gsm-lab-{lab.name}.yml")
        if os.path.exists(path):
            with open(path) as f:
                if not f.readline().startswith("# GENERATED by `gsm-lab tmuxinator`"):
                    sys.exit(f"[FAIL] {path} exists and was not generated by gsm-lab; not overwriting")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        print(f"wrote {path}; start with: tmuxinator start gsm-lab-{lab.name}")
    else:
        print(text, end="")
    return 0


def c_exec(args):
    """Run one instance in the foreground on this machine (for tmux/tmuxinator panes): place config,
    record state, exec. Ctrl-C stops it."""
    lab = load(args)
    resolve(lab, [args.instance])
    i = lab.instances[args.instance]
    if not lab.nodes[i.node].local:
        sys.exit(f"[FAIL] {args.instance} is placed on {i.node}; run this on that node")
    rt = Runtime(lab)
    st, pid = rt.status(args.instance)
    if st == "running":
        sys.exit(f"[FAIL] {args.instance} already running (pid {pid}); `gsm-lab stop {args.instance}` first")
    l = render.render(lab, args.instance)
    for d in [""] + l.dirs:
        os.makedirs(os.path.join(l.workdir, d), exist_ok=True)
    note = rt.place_config(l)
    print(f"[gsm-lab] {args.instance}: config {note}; exec {' '.join(l.argv)}", flush=True)
    import json
    os.chdir(l.workdir)
    with open(os.path.join(l.workdir, "state.json"), "w") as f:
        st = open(f"/proc/{os.getpid()}/stat").read().rsplit(")", 1)[1].split()[19]
        json.dump({"pid": os.getpid(), "starttime": st, "argv": l.argv, "backend": "exec", "lab": lab.name,
                   "instance": args.instance}, f)
    os.execvpe(l.argv[0], l.argv, {**os.environ, **l.env})


def c_provision(args):
    """(Re)provision HLR subscribers: `subscribers:` of each HLR plus every mobile SIM (idempotent).
    `start` does this automatically; needed after starting through tmuxinator / `exec`."""
    lab = load(args)
    rt = Runtime(lab)
    for hlr, subs in render.hlr_subscribers(lab).items():
        if not subs:
            continue
        if rt.status(hlr)[0] != "running":
            print(f"[SKIP] {hlr}: not running")
            continue
        rt.provision(hlr, subs)
    return 0


def c_setup(args):
    return repo.setup(recursive=args.recursive, build=args.build)


def c_update(args):
    return repo.update()


def c_repo_status(args):
    return repo.status()


def c_repo_auth(args):
    return 1 if repo.auth_report() else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="gsm-lab", description="Declarative manager for the Osmocom GSM lab.")
    p.add_argument("-f", "--file", default=DEFAULT_FILE, help=f"lab description (default: $GSM_LAB_FILE or {DEFAULT_FILE})")
    p.add_argument("-d", "--deployment", help="named placement from the lab file's deployments:")
    p.add_argument("-o", "--override", action="append", metavar="FILE",
                   help="YAML merged on top of the lab file (repeatable), e.g. a site's node definitions")
    p.add_argument("-v", "--verbose", action="store_true", help="show remote commands and info-level findings")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, fn, help, aliases=()):
        sp = sub.add_parser(name, help=help, aliases=list(aliases))
        sp.set_defaults(fn=fn)
        return sp

    sp = cmd("setup", c_setup, "phase 0: check out all submodules, check access/auth, check build prerequisites")
    sp.add_argument("--recursive", action="store_true", help="also the nested osmo-trx/osmocom-bb submodule")
    sp.add_argument("--build", action="store_true", help="then build everything (./gsm build)")
    cmd("update", c_update, "fast-forward the superproject; move only clean, unmodified submodules")
    cmd("repo-status", c_repo_status, "superproject + per-component checkout state")
    cmd("repo-auth", c_repo_auth, "check non-interactive access to every repository URL")

    sp = cmd("validate", c_validate, "check the lab description (no network access)")
    sp.add_argument("--strict", action="store_true", help="warnings fail too")
    sp = cmd("nodes", c_nodes, "list nodes; 'nodes --status' also checks reachability")
    sp.add_argument("--status", action="store_true")
    sp = cmd("topology", c_topology, "logical topology: networks, BSCs, BTSs, TRXs and where they run")
    sp.add_argument("--links", action="store_true", help="also every signalling/transport link and its endpoint")
    sp = cmd("services", c_services, "all instances with their endpoints", aliases=("instances",))
    sp.add_argument("--status", action="store_true")
    sp = cmd("render", c_render, "show (or --out DIR: write) the generated configs; changes nothing on nodes")
    sp.add_argument("instances", nargs="*")
    sp.add_argument("--out")
    for name, fn, h in (("start", c_start, "start instances (default: all) in dependency order"),
                        ("stop", c_stop, "stop instances (default: all); dependents first"),
                        ("restart", c_restart, "stop + start the given instances only")):
        sp = cmd(name, fn, h)
        sp.add_argument("instances", nargs="*")
        sp.add_argument("--backend", choices=("proc", "tmux"))
        sp.add_argument("--no-deps", action="store_true",
                        help="start: don't add dependencies; stop: don't also stop dependents")
        if name == "start":
            sp.add_argument("--dry-run", action="store_true", help="validate, preflight and show what would happen")
    sp = cmd("status", c_status, "process + VTY state of each instance")
    sp.add_argument("instances", nargs="*")
    sp = cmd("health", c_health, "process, VTY and link checks; --e2e adds registration + SMS per phone")
    sp.add_argument("instances", nargs="*")
    sp.add_argument("--e2e", action="store_true")
    sp.add_argument("--timeout", type=int, default=60)
    sp = cmd("logs", c_logs, "show an instance's output")
    sp.add_argument("instance")
    sp.add_argument("-n", "--lines", type=int, default=50)
    sp.add_argument("-f", "--follow", action="store_true")
    sp = cmd("doctor", c_doctor, "validation + node, binary, address, port and reachability checks")
    sp.add_argument("--backend", choices=("proc", "tmux"))
    sp = cmd("tmuxinator", c_tmuxinator, "export a tmuxinator project driven by this lab file")
    sp.add_argument("--write", action="store_true", help="write ~/.config/tmuxinator/gsm-lab-<lab>.yml")
    cmd("provision", c_provision, "provision HLR subscribers (SIMs of the lab's phones + subscribers:)")
    sp = cmd("exec", c_exec, "run one local instance in the foreground (used by tmux/tmuxinator panes)")
    sp.add_argument("instance")

    args = p.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)   # keep order with output of child processes (./gsm)
    nodes.VERBOSE = args.verbose and args.cmd not in ("validate",)
    if args.cmd in ("setup", "update", "repo-status", "repo-auth"):
        return args.fn(args)
    if not os.path.exists(args.file):
        sys.exit(f"[FAIL] lab file {args.file} not found (use -f FILE or set GSM_LAB_FILE; examples in lab/topologies/)")
    try:
        return args.fn(args) or 0
    except nodes.NodeError as e:
        sys.exit(f"[FAIL] {e}")
    except KeyboardInterrupt:
        return 130
