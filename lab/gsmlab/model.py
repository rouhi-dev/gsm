"""Load and normalise a lab description.

A lab is one YAML file (optionally split with `include:` and overridden by `<file>.local.yaml`). It has
separate top-level sections for separate concerns:

    lab:          name, defaults (install prefix, run dir, backend)
    nodes:        physical/virtual Linux machines -- connection data only, never a role
    plmns:        network identities (MCC/MNC as strings) -- never inside a node
    instances:    runtime instances of a service (stp/hlr/mgw/msc/bsc/bts/trx/virtphy/mobile), their
                  node, addresses and ports
    networks:     logical GSM topology: PLMN, STP, MSC/HLR/MGW, BSCs -> BTSs -> TRXs, cell identity
    deployments:  named placements (instance -> node) that override `instances.*.node`

Loading never touches the network or the filesystem beyond reading the YAML files. Structural problems
(wrong types, unknown keys) raise LabError; semantic problems are left to validate.py, so that one run
reports all of them.
"""

import copy
import getpass
import os
import re
from dataclasses import dataclass, field

import yaml

from .services import SERVICES, VIRT_UM_DEFAULTS


class LabError(Exception):
    pass


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "local"}


# ---------------------------------------------------------------- YAML loading (include + local override)
def _deep_merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def _read_yaml(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except OSError as e:
        raise LabError(f"cannot read {path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise LabError(f"{path}: invalid YAML: {e}") from None
    if not isinstance(data, dict):
        raise LabError(f"{path}: top level must be a mapping")
    return data


def load_raw(path, _seen=None):
    """Read a lab file, resolve `include:` (relative paths, merged in order, the including file wins)
    and merge `<name>.local.yaml` next to the top-level file (for per-site overrides, not committed)."""
    path = os.path.abspath(path)
    seen = _seen if _seen is not None else set()
    if path in seen:
        raise LabError(f"include loop at {path}")
    seen.add(path)
    data = _read_yaml(path)
    merged = {}
    for inc in data.pop("include", []) or []:
        merged = _deep_merge(merged, load_raw(os.path.join(os.path.dirname(path), inc), seen))
    merged = _deep_merge(merged, data)
    if _seen is None:
        local = re.sub(r"\.ya?ml$", "", path) + ".local.yaml"
        if os.path.exists(local):
            merged = _deep_merge(merged, _read_yaml(local))
            merged.setdefault("_sources", []).append(local)
        merged.setdefault("_sources", []).insert(0, path)
    return merged


# ---------------------------------------------------------------- normalised model
@dataclass
class Node:
    id: str
    host: str
    local: bool
    address: str | None          # lab-facing address other nodes use to reach services here
    ssh_user: str | None
    ssh_port: int
    ssh_options: list
    home: str
    prefix: str                  # Osmocom install prefix: <prefix>/bin, <prefix>/lib
    run_dir: str                 # per-lab runtime root on that node; instance dirs below it
    extra_addresses: list        # addresses the lab expects to exist on the node (validated live)
    named_addresses: dict        # name -> IP; instances/deployments may bind to a name (e.g. 'abis2')
    mgmt_address: str            # default bind address of VTY/CTRL (management) interfaces
    raw: dict

    @property
    def machine(self):
        """Nodes with the same machine key share one kernel: ports and addresses collide between them."""
        return "localhost" if self.local else self.host


@dataclass
class Plmn:
    id: str
    name: str
    mcc: str
    mnc: str
    mnc_length: int


@dataclass
class Endpoint:
    ip: str
    port: int
    proto: str


@dataclass
class Instance:
    id: str
    service: str
    node: str | None
    variant: str | None
    bind: str | None             # default address for this instance's listeners and client sockets
    vty: Endpoint | None         # None: the program has no VTY (virtphy)
    ctrl: Endpoint | None
    ports: dict                  # name -> Endpoint (service listeners and fixed client ports)
    opts: dict                   # service-specific settings (point_code, rtp range, virtual_um, sims, ...)
    raw: str                     # verbatim VTY appended to the generated config (escape hatch)
    args: list                   # extra command-line arguments
    config_file: str | None      # use this hand-written cfg instead of generating one
    enabled: bool
    log_level: str


@dataclass
class Trx:
    index: int
    arfcn: int
    phy: str | None              # osmo-trx instance (for bts variant 'trx')
    channel: int
    nominal_power: int
    max_power_red: int
    timeslots: list


@dataclass
class Cell:                      # a BTS as seen by its BSC (logical) -- the process is Instance(bts)
    id: str
    bsc: str
    number: int                  # 'bts N' index inside the BSC
    band: str
    lac: int
    ci: int
    bsic: int
    unit_id: int
    trx: list


@dataclass
class BscLink:
    id: str
    mgw: str | None
    ran_plmn: str | None         # PLMN this BSC broadcasts if different from the network (MSC) PLMN
    cells: list


@dataclass
class Network:
    id: str
    plmn: str
    stp: str | None
    msc: str | None
    hlr: str | None
    mgw: str | None
    bscs: list
    short_name: str
    long_name: str
    authentication: str
    encryption: str
    routing: str                 # dynamic | static (M3UA routing keys on the STP)


@dataclass
class Lab:
    name: str
    sources: list
    backend: str
    nodes: dict = field(default_factory=dict)
    plmns: dict = field(default_factory=dict)
    instances: dict = field(default_factory=dict)
    networks: dict = field(default_factory=dict)
    deployments: dict = field(default_factory=dict)
    deployment: str | None = None
    load_errors: list = field(default_factory=list)   # (path, message) found while normalising

    # -------- convenience lookups
    def network_of(self, inst_id):
        """Networks an instance takes part in (shared instances may appear in several)."""
        out = []
        for n in self.networks.values():
            ids = {n.stp, n.msc, n.hlr, n.mgw}
            for b in n.bscs:
                ids |= {b.id, b.mgw}
                for c in b.cells:
                    ids.add(c.id)
                    ids |= {t.phy for t in c.trx}
            if inst_id in ids:
                out.append(n)
        return out

    def cells(self):
        for n in self.networks.values():
            for b in n.bscs:
                for c in b.cells:
                    yield n, b, c

    def cell(self, bts_id):
        for n, b, c in self.cells():
            if c.id == bts_id:
                return n, b, c
        return None

    def bsc_links(self, bsc_id):
        return [(n, b) for n in self.networks.values() for b in n.bscs if b.id == bsc_id]

    def instance_dir(self, inst):
        return f"{self.nodes[inst.node].run_dir}/{inst.id}" if inst.node in self.nodes else f"<unplaced>/{inst.id}"


# ---------------------------------------------------------------- helpers
def _err(lab, path, msg):
    lab.load_errors.append((path, msg))


def _known(lab, path, d, keys):
    for k in d:
        if k not in keys:
            _err(lab, path, f"unknown key '{k}' (allowed: {', '.join(sorted(keys))})")


def _as_dict(lab, path, v):
    if v is None:
        return {}
    if not isinstance(v, dict):
        _err(lab, path, f"must be a mapping, got {type(v).__name__}")
        return {}
    return v


def _int(lab, path, v, default=None):
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, str) and re.fullmatch(r"0x[0-9a-fA-F]+|\d+", v):
            return int(v, 0)
        _err(lab, path, f"must be an integer, got {v!r}")
        return default
    return v


def _str_code(lab, path, v):
    """MCC/MNC: must be written as a string so leading zeros survive YAML ("001", not 001)."""
    if isinstance(v, int) and not isinstance(v, bool):
        _err(lab, path, f"write MCC/MNC as a quoted string to keep leading zeros (got the number {v})")
        return str(v)
    if not isinstance(v, str):
        _err(lab, path, f"must be a quoted string of digits, got {v!r}")
        return ""
    return v


def _endpoint(lab, path, spec, default_ip, default_port, proto):
    """Accept `port: N`, `{ip:, port:}` or `"ip:port"`. The protocol is a property of the service."""
    if spec is None:
        return Endpoint(default_ip, default_port, proto)
    if isinstance(spec, int) and not isinstance(spec, bool):
        return Endpoint(default_ip, spec, proto)
    if isinstance(spec, str) and ":" in spec:
        ip, _, port = spec.rpartition(":")
        return Endpoint(ip, _int(lab, path, port, default_port), proto)
    if isinstance(spec, dict):
        _known(lab, path, spec, {"ip", "port"})
        return Endpoint(spec.get("ip", default_ip), _int(lab, f"{path}.port", spec.get("port"), default_port), proto)
    _err(lab, path, f"endpoint must be a port, 'ip:port' or {{ip:, port:}}, got {spec!r}")
    return Endpoint(default_ip, default_port, proto)


# ---------------------------------------------------------------- normalisation
# Keys every config-file based instance accepts, then what each service adds. Anything else is rejected:
# a key gsm-lab does not implement must never be accepted and silently ignored.
GENERIC_KEYS = {"service", "node", "bind", "raw", "args", "config_file", "enabled", "log_level"}
SERVICE_KEYS = {
    "stp": GENERIC_KEYS | {"vty", "ports", "point_code", "transport"},
    "hlr": GENERIC_KEYS | {"vty", "ctrl", "ports", "db", "subscribers"},
    "mgw": GENERIC_KEYS | {"vty", "ctrl", "ports", "rtp_range", "rtp_bind", "endpoints"},
    "msc": GENERIC_KEYS | {"vty", "ctrl", "ports", "point_code", "m3ua_local_port"},
    "bsc": GENERIC_KEYS | {"vty", "ctrl", "ports", "point_code", "m3ua_local_port"},
    "bts": GENERIC_KEYS | {"vty", "ctrl", "variant", "virtual_um", "trx_options"},
    "trx": GENERIC_KEYS | {"vty", "ctrl", "ports", "variant", "dev_args", "trx_options"},
    "mobile": GENERIC_KEYS | {"vty", "virtphy", "sims"},
    "virtphy": {"service", "node", "args", "enabled", "virtual_um"},   # no config file, no VTY
}
VARIANT_KEYS = {("bts", "virtual"): {"virtual_um"}, ("bts", "trx"): {"trx_options"}}   # only for that variant
VIRT_UM_KEYS = {"bts": {"ms_group", "ms_port", "bts_group", "bts_port", "netdev", "ttl"},
                "virtphy": {"ms_group", "ms_port", "bts_group", "netdev", "ttl"}}     # virtphy: one port (-x)
SIM_KEYS = {"imsi", "ki", "msisdn", "imei", "virtphy"}
SUBSCRIBER_KEYS = {"imsi", "ki", "msisdn"}
LOG_LEVELS = {"debug", "info", "notice", "error", "fatal"}
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:_-]*")
USER_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
VTY_TOKEN_RE = re.compile(r"[a-z][a-z0-9-]*")
NETWORK_KEYS = {"plmn", "stp", "msc", "hlr", "mgw", "bscs", "short_name", "long_name", "authentication",
                "encryption", "routing"}
CELL_KEYS = {"band", "lac", "ci", "bsic", "unit_id", "trx", "number"}
TRX_KEYS = {"arfcn", "phy", "channel", "nominal_power", "max_power_red", "timeslots"}
DEFAULT_TS = ["CCCH+SDCCH4", "SDCCH8", "TCH/F", "TCH/F", "TCH/F", "TCH/F", "TCH/F", "TCH/F"]


_LINE_BREAKS = {"\x85", "\u2028", "\u2029"}


def _bad_chars(text, allow=""):
    return sorted({repr(c) for c in text if (ord(c) < 0x20 or ord(c) == 0x7f or c in _LINE_BREAKS) and c not in allow})


def _scan_chars(lab, path, v):
    """Every value may end up in a generated Osmocom config (one VTY command per line) or in a shell
    command: reject control characters and line breaks everywhere. Only `instances.<id>.raw` -- verbatim
    VTY by design -- may contain newlines (and tabs)."""
    if isinstance(v, dict):
        for k, x in v.items():
            kp = f"{path}.{k}" if path else str(k)
            bad = _bad_chars(str(k))
            if bad:
                _err(lab, path or "<top>", f"key {str(k)!r} contains control characters {', '.join(bad)}")
            _scan_chars(lab, kp, x)
    elif isinstance(v, list):
        for n, x in enumerate(v):
            _scan_chars(lab, f"{path}[{n}]", x)
    elif isinstance(v, str):
        allow = "\n\t" if re.fullmatch(r"instances\.[^.]+\.raw", path) else ""
        bad = _bad_chars(v, allow)
        if bad:
            _err(lab, path, f"contains control characters / line breaks {', '.join(bad)}: values are written into "
                            "Osmocom configs and shell commands" + ("" if allow else "; multi-line VTY belongs in instances.<id>.raw"))


def _check_id(lab, path, ident):
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        _err(lab, path, f"identifier {ident!r} must be 1-63 letters, digits, '.', '_' or '-' (starting with a letter "
                        "or digit): it is used in file names, VTY object names and shell commands")


def build(raw, deployment=None):
    raw = copy.deepcopy(raw)
    sources = raw.pop("_sources", [])
    lab_sec = raw.pop("lab", None) or {}
    bad_lab_sec = not isinstance(lab_sec, dict)
    lab_sec = {} if bad_lab_sec else lab_sec
    lab = Lab(name=str(lab_sec.get("name", "lab")), sources=sources, backend=lab_sec.get("backend", "proc"))
    if bad_lab_sec:
        _err(lab, "lab", "must be a mapping")
    _scan_chars(lab, "", {"lab": lab_sec, **raw})
    for k in [k for k in raw if str(k).startswith("x-")]:   # free-form keys for YAML anchors (&name)
        raw.pop(k)
    _known(lab, "", raw, {"nodes", "plmns", "instances", "networks", "deployments"})
    _known(lab, "lab", lab_sec, {"name", "prefix", "run_dir", "backend", "deployment"})
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", lab.name):
        _err(lab, "lab.name", "use letters, digits, '.', '_' or '-' (it becomes a directory and tmux session name)")
    if lab.backend not in ("proc", "tmux"):
        _err(lab, "lab.backend", f"must be 'proc' or 'tmux', got {lab.backend!r}")
    default_prefix = lab_sec.get("prefix", os.path.join(REPO_ROOT, "_install"))

    # ---- nodes
    for nid, n in _as_dict(lab, "nodes", raw.get("nodes")).items():
        p = f"nodes.{nid}"
        _check_id(lab, p, nid)
        n = _as_dict(lab, p, n)
        _known(lab, p, n, {"host", "address", "mgmt_address", "ssh", "home", "prefix", "run_dir", "addresses",
                           "description"})   # description: free text for humans, never used
        host = str(n.get("host", "localhost"))
        if not HOST_RE.fullmatch(host):    # also stops option injection into ssh (a host starting with '-')
            _err(lab, f"{p}.host", f"'{host}' is not a valid host name or address")
        local = host in LOCAL_HOSTS
        ssh = _as_dict(lab, f"{p}.ssh", n.get("ssh"))
        _known(lab, f"{p}.ssh", ssh, {"user", "port", "options"})
        user = ssh.get("user") or (getpass.getuser() if local else None)
        if ssh.get("user") is not None and not USER_RE.fullmatch(str(ssh["user"])):
            _err(lab, f"{p}.ssh.user", f"'{ssh['user']}' is not a valid user name")
        if not isinstance(ssh.get("options", []), list):
            _err(lab, f"{p}.ssh.options", "must be a list of ssh arguments, e.g. [-i, ~/.ssh/lab_key]")
            ssh["options"] = []
        home = n.get("home") or (os.path.expanduser("~") if local else f"/home/{user or getpass.getuser()}")
        run_dir = n.get("run_dir", lab_sec.get("run_dir", "~/.local/state/gsm-lab/{lab}"))
        run_dir = run_dir.replace("{lab}", lab.name)
        run_dir = home + run_dir[1:] if run_dir.startswith("~") else run_dir
        prefix = n.get("prefix", default_prefix)
        prefix = home + prefix[1:] if prefix.startswith("~") else prefix
        address = n.get("address") or ("127.0.0.1" if local else None)
        addrs = n.get("addresses") or []
        named = dict(addrs) if isinstance(addrs, dict) else {}
        extra = list(named.values()) if isinstance(addrs, dict) else list(addrs)
        lab.nodes[nid] = Node(nid, host, local, address, user, _int(lab, f"{p}.ssh.port", ssh.get("port"), 22),
                              list(ssh.get("options", [])), home, prefix, run_dir,
                              extra, named, str(n.get("mgmt_address", "127.0.0.1")), n)

    # ---- plmns
    for pid, pl in _as_dict(lab, "plmns", raw.get("plmns")).items():
        p = f"plmns.{pid}"
        _check_id(lab, p, pid)
        pl = _as_dict(lab, p, pl)
        _known(lab, p, pl, {"name", "mcc", "mnc", "mnc_length"})
        mcc, mnc = _str_code(lab, f"{p}.mcc", pl.get("mcc")), _str_code(lab, f"{p}.mnc", pl.get("mnc"))
        lab.plmns[pid] = Plmn(pid, str(pl.get("name", pid)), mcc, mnc,
                              _int(lab, f"{p}.mnc_length", pl.get("mnc_length"), len(mnc)))

    # ---- deployments (placement overrides)
    for did, d in _as_dict(lab, "deployments", raw.get("deployments")).items():
        _check_id(lab, f"deployments.{did}", did)
        d = _as_dict(lab, f"deployments.{did}", d)
        for iid, v in d.items():   # 'node-01' or {node: node-01, bind: <ip or node address name>}
            if isinstance(v, dict):
                _known(lab, f"deployments.{did}.{iid}", v, {"node", "bind"})
            elif not isinstance(v, str):
                _err(lab, f"deployments.{did}.{iid}", "must be a node id or {node:, bind:}")
        lab.deployments[did] = d
    lab.deployment = deployment or lab_sec.get("deployment")
    placement = {}
    if lab.deployment:
        if lab.deployment not in lab.deployments:
            _err(lab, "lab.deployment", f"unknown deployment '{lab.deployment}' (have: {', '.join(lab.deployments) or 'none'})")
        else:
            placement = lab.deployments[lab.deployment]

    # ---- instances
    for iid, i in _as_dict(lab, "instances", raw.get("instances")).items():
        p = f"instances.{iid}"
        _check_id(lab, p, iid)
        i = _as_dict(lab, p, i)
        svc_name = i.get("service")
        svc = SERVICES.get(svc_name)
        if not svc:
            _err(lab, f"{p}.service", f"unknown service {svc_name!r} (one of: {', '.join(SERVICES)})")
            continue
        allowed = SERVICE_KEYS[svc_name]
        for k in i:
            if k not in allowed:
                _err(lab, f"{p}.{k}", f"not supported for service {svc_name} (allowed: {', '.join(sorted(allowed))})")
        place = placement.get(iid)
        place = place if isinstance(place, dict) else ({"node": place} if place else {})
        node = place.get("node", i.get("node"))
        bind = place.get("bind", i.get("bind"))
        if node in lab.nodes and bind in lab.nodes[node].named_addresses:
            bind = lab.nodes[node].named_addresses[bind]
        elif bind and not re.fullmatch(r"[0-9a-fA-F:.]+", str(bind)):
            _err(lab, f"{p}.bind", f"'{bind}' is neither an IP nor an address name of node {node} "
                                   f"(has: {', '.join(lab.nodes[node].named_addresses) if node in lab.nodes else '-'})")
            bind = None
        bind = bind or (lab.nodes[node].address if node in lab.nodes else None)
        # management interfaces: the node's mgmt address, unless the instance itself binds to a loopback
        # address (then VTY/CTRL follow it, so instances separated by 127.0.0.x need no extra ports)
        mgmt = bind if bind and bind.startswith("127.") else (
            lab.nodes[node].mgmt_address if node in lab.nodes else "127.0.0.1")
        vty = _endpoint(lab, f"{p}.vty", i.get("vty"), mgmt, svc.vty_port, "tcp") if svc.vty_port else None
        ctrl = (_endpoint(lab, f"{p}.ctrl", i.get("ctrl"), mgmt, svc.ctrl_port, "tcp")
                if svc.ctrl_port else None)
        ports_spec = _as_dict(lab, f"{p}.ports", i.get("ports"))
        ports = {}
        for port in svc.ports:
            ports[port.name] = _endpoint(lab, f"{p}.ports.{port.name}", ports_spec.get(port.name),
                                         bind, port.default, port.proto)
        for k in ports_spec:
            if k not in ports:
                _err(lab, f"{p}.ports.{k}", f"service {svc_name} has no port '{k}' (has: {', '.join(ports) or 'none'})")
        variant = i.get("variant")
        if svc.variants:
            variant = variant or next(iter(svc.variants))
            if variant not in svc.variants:
                _err(lab, f"{p}.variant", f"unknown variant {variant!r} for {svc_name} (one of: {', '.join(svc.variants)})")
        for (vs, vv), keys in VARIANT_KEYS.items():
            if vs == svc_name and vv != variant:
                for k in keys & set(i):
                    _err(lab, f"{p}.{k}", f"only used by {svc_name} variant '{vv}', this instance is '{variant}'")
        lvl = i.get("log_level", "notice")
        if lvl not in LOG_LEVELS:
            _err(lab, f"{p}.log_level", f"must be one of {', '.join(sorted(LOG_LEVELS))}, got {lvl!r}")
        if svc_name == "stp" and i.get("transport", "tcp") not in ("tcp", "sctp"):
            _err(lab, f"{p}.transport", "must be 'tcp' or 'sctp'")
        if svc_name == "bts" and "trx_options" in i:
            _known(lab, f"{p}.trx_options", _as_dict(lab, f"{p}.trx_options", i["trx_options"]), {"local_base_port"})
        if svc_name == "trx":
            for k, v in _as_dict(lab, f"{p}.trx_options", i.get("trx_options")).items():
                if not VTY_TOKEN_RE.fullmatch(str(k)):
                    _err(lab, f"{p}.trx_options.{k}", "must be an osmo-trx 'trx' node command name (e.g. clock-ref)")
                if isinstance(v, (dict, list)):
                    _err(lab, f"{p}.trx_options.{k}", "value must be a single word or number")
        for k, sim in enumerate(i.get("sims") or []):
            _known(lab, f"{p}.sims[{k}]", _as_dict(lab, f"{p}.sims[{k}]", sim), SIM_KEYS)
        for k, sub in enumerate(i.get("subscribers") or []):
            _known(lab, f"{p}.subscribers[{k}]", _as_dict(lab, f"{p}.subscribers[{k}]", sub), SUBSCRIBER_KEYS)
        opts = {k: v for k, v in i.items() if k not in
                {"service", "node", "variant", "bind", "vty", "ctrl", "ports", "raw", "args", "config_file",
                 "enabled", "log_level"}}
        if svc_name == "bts" and variant == "virtual" or svc_name == "virtphy":
            vu = _as_dict(lab, f"{p}.virtual_um", opts.get("virtual_um"))
            _known(lab, f"{p}.virtual_um", vu, VIRT_UM_KEYS[svc_name])
            opts["virtual_um"] = {**VIRT_UM_DEFAULTS, **vu}
        lab.instances[iid] = Instance(
            iid, svc_name, node, variant, bind, vty, ctrl, ports, opts,
            str(i.get("raw", "") or ""), [str(a) for a in i.get("args", [])], i.get("config_file"),
            bool(i.get("enabled", True)), str(i.get("log_level", "notice")))
    for iid in placement:
        if iid not in lab.instances:
            _err(lab, f"deployments.{lab.deployment}.{iid}", "places an instance that is not defined")

    # ---- networks
    for nid, n in _as_dict(lab, "networks", raw.get("networks")).items():
        p = f"networks.{nid}"
        _check_id(lab, p, nid)
        n = _as_dict(lab, p, n)
        _known(lab, p, n, NETWORK_KEYS)
        bscs = []
        for bid, b in _as_dict(lab, f"{p}.bscs", n.get("bscs")).items():
            bp = f"{p}.bscs.{bid}"
            b = _as_dict(lab, bp, b)
            _known(lab, bp, b, {"mgw", "ran_plmn", "bts"})
            cells = []
            for num, (cid, c) in enumerate(_as_dict(lab, f"{bp}.bts", b.get("bts")).items()):
                cp = f"{bp}.bts.{cid}"
                c = _as_dict(lab, cp, c)
                _known(lab, cp, c, CELL_KEYS)
                trx_list = c.get("trx") or [{}]
                if not isinstance(trx_list, list):
                    _err(lab, f"{cp}.trx", "must be a list of TRX definitions")
                    trx_list = [{}]
                trxs = []
                for ti, t in enumerate(trx_list):
                    tp = f"{cp}.trx[{ti}]"
                    t = _as_dict(lab, tp, t)
                    _known(lab, tp, t, TRX_KEYS)
                    trxs.append(Trx(ti, _int(lab, f"{tp}.arfcn", t.get("arfcn")), t.get("phy"),
                                    _int(lab, f"{tp}.channel", t.get("channel"), ti),
                                    _int(lab, f"{tp}.nominal_power", t.get("nominal_power"), 23),
                                    _int(lab, f"{tp}.max_power_red", t.get("max_power_red"), 20),
                                    list(t.get("timeslots") or DEFAULT_TS)))
                cells.append(Cell(cid, bid, _int(lab, f"{cp}.number", c.get("number"), num),
                                  str(c.get("band", "DCS1800")), _int(lab, f"{cp}.lac", c.get("lac")),
                                  _int(lab, f"{cp}.ci", c.get("ci")), _int(lab, f"{cp}.bsic", c.get("bsic"), 63),
                                  _int(lab, f"{cp}.unit_id", c.get("unit_id")), trxs))
            bscs.append(BscLink(bid, b.get("mgw") or n.get("mgw"), b.get("ran_plmn"), cells))
        lab.networks[nid] = Network(
            nid, n.get("plmn"), n.get("stp"), n.get("msc"), n.get("hlr"), n.get("mgw"), bscs,
            str(n.get("short_name", nid)), str(n.get("long_name", n.get("short_name", nid))),
            str(n.get("authentication", "optional")), str(n.get("encryption", "a5 0")),
            str(n.get("routing", "dynamic")))
    return lab


def load(path, deployment=None, overrides=()):
    """overrides: extra YAML files deep-merged on top, in order (e.g. a site's node definitions)."""
    raw = load_raw(path)
    for o in overrides:
        raw = _deep_merge(raw, load_raw(o))
        raw["_sources"] = raw.get("_sources", []) + [os.path.abspath(o)]
    return build(raw, deployment)
