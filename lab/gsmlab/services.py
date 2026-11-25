"""Service catalogue: what each Osmocom component is, independent of where it runs.

Every fact here was checked against the pinned sources in libs/ and apps/ (Aug-2026 release set):
default ports from libosmocore include/osmocom/{vty,ctrl}/ports.h, the rest from each program's VTY code.
A port marked fixed=True has NO configuration command in that release: two instances on the same node
can then only coexist on different IP addresses (e.g. 127.0.0.2 next to 127.0.0.1).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Port:
    name: str            # key used in instance config, e.g. "mgcp"
    proto: str           # tcp | udp | sctp
    default: int
    fixed: bool = False  # no VTY/CLI option to change it in this release
    note: str = ""


@dataclass(frozen=True)
class Service:
    name: str
    binary: str                      # default binary name (variant may override)
    vty_port: int
    ctrl_port: int | None
    ports: tuple = ()                # listening ports besides VTY/CTRL
    variants: dict = field(default_factory=dict)   # variant -> binary
    needs_config: bool = True        # takes -c <cfg>
    description: str = ""


SERVICES = {
    "stp": Service(
        "stp", "osmo-stp", 4239, None,
        (Port("m3ua", "tcp", 2905, note="M3UA listener (tcp or sctp, per instance 'transport')"),),
        description="OsmoSTP: SS7 signalling transfer point, routes SCCP/M3UA between BSCs and MSCs"),
    "hlr": Service(
        "hlr", "osmo-hlr", 4258, 4259,
        (Port("gsup", "tcp", 4222, fixed=True,
              note="osmo-hlr 1.9.4 has 'gsup / bind ip' but no port command"),),
        description="OsmoHLR: subscriber database (SQLite), GSUP server for MSCs"),
    "mgw": Service(
        "mgw", "osmo-mgw", 4243, 4267,
        (Port("mgcp", "udp", 2427),),
        description="OsmoMGW: RTP media gateway, MGCP-controlled by BSCs and MSCs"),
    "msc": Service(
        "msc", "osmo-msc", 4254, 4255,
        (Port("sgs", "sctp", 29118, note="SGs listener, always opened; needs kernel SCTP"),
         Port("mgcp_client", "udp", 2728, note="local port of the MGCP client towards its MGW")),
        description="OsmoMSC: MSC/VLR/SMSC; one PLMN per instance"),
    "bsc": Service(
        "bsc", "osmo-bsc", 4242, 4249,
        (Port("abis_oml", "tcp", 3002, fixed=True, note="IPA OML, 'ipa bind' sets the IP only"),
         Port("abis_rsl", "tcp", 3003, fixed=True, note="IPA RSL, fixed like OML"),
         Port("mgcp_client", "udp", 2727, note="local port of the MGCP client towards its MGW")),
        description="OsmoBSC: controls BTSs over Abis/IP, talks BSSMAP to the MSC over the A interface"),
    "bts": Service(
        "bts", "osmo-bts-virtual", 4241, 4238, (),
        variants={"virtual": "osmo-bts-virtual", "trx": "osmo-bts-trx"},
        description="OsmoBTS: one cell; virtual (UDP multicast Um, no radio) or trx (with osmo-trx)"),
    "trx": Service(
        "trx", "osmo-trx-uhd", 4237, 4236,
        (Port("trxd", "udp", 5700, note="base port; uses base .. base+2*channels+2"),),
        variants={"uhd": "osmo-trx-uhd", "lms": "osmo-trx-lms", "ipc": "osmo-trx-ipc",
                  "blade": "osmo-trx-blade"},
        description="OsmoTRX: SDR transceiver for osmo-bts-trx"),
    "virtphy": Service(
        "virtphy", "virtphy", 0, None, (), needs_config=False,
        description="osmocom-bb virtual L1 for a mobile on the virtual Um (UDP multicast)"),
    "mobile": Service(
        "mobile", "mobile", 4247, None, (),
        description="osmocom-bb mobile: layer 2/3 of a phone with a test SIM"),
}

# Start order (lower first). Within one tier, order is the instance declaration order.
TIER = {"stp": 0, "hlr": 0, "mgw": 0, "msc": 1, "bsc": 2, "trx": 2, "bts": 3, "virtphy": 4, "mobile": 5}

# Default virtual-Um multicast settings of osmo-bts-virtual / virtphy (osmo-bts virtual_um.h, virtphy)
VIRT_UM_DEFAULTS = {"ms_group": "239.193.23.1", "ms_port": 4729, "bts_group": "239.193.23.2", "bts_port": 4729}
