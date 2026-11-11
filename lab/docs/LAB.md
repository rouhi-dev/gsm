# gsm-lab: managing the multi-node, multi-PLMN Osmocom lab

`gsm-lab` turns one declarative YAML file into a running GSM network. The physical machines, the PLMNs,
the logical GSM topology, and which instance runs where are separate sections, so you can rearrange any
of them by changing configuration only.

```
./gsm-lab validate                 # check the lab file (offline)
./gsm-lab topology --links         # what connects to what, and where it runs
./gsm-lab start                    # start everything in dependency order (or: start bsc-02)
./gsm-lab health --e2e             # process -> VTY -> links -> phone registers + SMS round trip
./gsm-lab stop
```

Contents: [1 Architecture](#1-architecture) · [2 Logical vs physical](#2-logical-versus-physical-topology) ·
[3 Nodes](#3-nodes) · [4 PLMNs](#4-plmns) · [5 MSC/BSC/BTS/TRX](#5-mscbscbtstrx-relationships) ·
[6 STP and signalling](#6-osmostp-and-signalling) · [7 Endpoints and ports](#7-endpoints-and-ports) ·
[8 Installation](#8-installation-and-dependencies) · [9 Authentication and submodules](#9-authentication-and-submodules) ·
[10 Running and monitoring](#10-starting-stopping-monitoring) · [11 Adding a node](#11-adding-a-physical-node) ·
[12 Moving services](#12-moving-services-between-nodes) · [13 New PLMN](#13-creating-a-new-plmn) ·
[14 Second BSC](#14-a-second-bsc-on-an-existing-msc) · [15 Troubleshooting](#15-troubleshooting) ·
[What Osmocom supports](#what-osmocom-supports) · [Verification status](#verification-status) ·
[Schema reference](#schema-reference)

---

## 1. Architecture

```
lab file (YAML) ──► model.py ──► validate.py ──► render.py ──► runtime.py ──► nodes.py ──► node (local / SSH)
  nodes             normalise     references,     one .cfg per   order, pidfiles,   bash snippets      Osmocom
  plmns                           PLMN, point     instance from  start/stop,        (ssh BatchMode)    processes
  instances                       codes, ports,   the topology   tmux/proc backend
  networks                        capabilities                   health.py: VTY checks + end-to-end
  deployments
```

* **One source of truth.** The lab file is authoritative. Osmocom configs are generated from it (never
  edited by hand), and the tmuxinator project is exported from it.
* **Lightweight.** Python 3 + PyYAML on the control machine. Each node only needs bash, python3, `ss`
  and `ip`, plus `tmux` if you use the tmux backend. No daemon, no containers.
* **Builds on `./gsm`.** `./gsm` checks out, pins, builds and verifies the sources; `gsm-lab` runs the
  result.

Code: `lab/gsmlab/` (`model`, `services`, `topology`, `validate`, `render`, `nodes`, `runtime`,
`health`, `repo`, `cli`). Entry point: `./gsm-lab`.

## 2. Logical versus physical topology

| Concern | Section | Contains | Must not contain |
|---|---|---|---|
| Physical machines | `nodes:` | host, SSH, addresses, install prefix, run dir | roles, PLMNs |
| Network identity | `plmns:` | MCC/MNC (strings) | anything physical |
| Runtime instances | `instances:` | service, variant, addresses, ports, point code, extra VTY | relationships |
| Logical GSM topology | `networks:` | PLMN; STP/MSC/HLR/MGW; BSCs → BTSs → TRXs; cell identity | where anything runs |
| Placement | `instances.*.node` or `deployments:` | instance → node (+ optional address) | identities |
| Site overrides | `-o FILE`, `<lab>.local.yaml` | e.g. different node addresses | — |

A **node** never has a role. Any node can host any mix of instances, and any number of instances of the
same service as long as their sockets don't collide (the validator checks this; see section 7).

The same logical network can run in different placements. `topologies/multi-bsc.yaml` has one network and
four deployments (A–D from the brief), selected with `-d`:

```
gsm-lab -f lab/topologies/multi-bsc.yaml -d split topology        # core | 2 BSCs | 4 BTSs
gsm-lab -f lab/topologies/multi-bsc.yaml -d bsc-per-node topology # core | BSC+BTS | BSC+2 BTS
```

## 3. Nodes

```yaml
nodes:
  node-02:
    host: 192.168.50.12          # 'localhost' = run locally; anything else = SSH
    address: 192.168.50.12       # address other nodes use to reach services here (default bind)
    addresses: { addr2: 192.168.50.22 }   # optional extra addresses, by name
    mgmt_address: 127.0.0.1      # default bind of VTY/CTRL (default 127.0.0.1)
    ssh: { user: lab, port: 22, options: [-i, ~/.ssh/lab_ed25519] }
    prefix: /opt/osmocom         # Osmocom install prefix on that node (default: this repo's _install)
    run_dir: ~/.local/state/gsm-lab/{lab}   # per-instance directories are created below it
    home: /home/lab              # used to expand ~ for remote nodes (default /home/<ssh user>)
```

* SSH runs in `BatchMode`: keys or agent only. gsm-lab never asks for or stores passwords. Test with
  `gsm-lab nodes --status`.
* Per instance on the node: `<run_dir>/<instance>/` holds `<instance>.cfg` (generated), `state.json`
  (pid, start time, argv), `out.log`, and the instance's own files (`hlr.db`, `sms.db`, sockets).
* `gsm-lab doctor` probes every node for binaries, addresses, free ports, kernel SCTP, python3 and tmux.

## 4. PLMNs

```yaml
plmns:
  operator_a: { name: Operator A, mcc: "432", mnc: "11" }
  operator_b: { name: Operator B, mcc: "001", mnc: "002", mnc_length: 3 }
```

MCC and MNC **must be quoted strings**. A YAML number loses leading zeros, and `"02"` and `"002"` are
different PLMNs, so the validator rejects numbers. `mnc_length` defaults to the number of digits written;
if you set it, it must match. Duplicate MCC/MNC pairs are rejected.

A PLMN is referenced only by `networks.<id>.plmn` (and optionally `bscs.<id>.ran_plmn`, see
[What Osmocom supports](#what-osmocom-supports)). Moving instances between nodes never touches it.

## 5. MSC/BSC/BTS/TRX relationships

```yaml
networks:
  net-a:
    plmn: operator_a
    stp: stp-01          # SS7 domain (M3UA/SCCP routing)
    msc: msc-01          # exactly one MSC per network = per PLMN core
    hlr: hlr-01
    mgw: mgw-01          # MSC's MGW; also the BSCs' default
    authentication: required          # or optional (default)
    routing: dynamic                  # or static (see section 6)
    bscs:
      bsc-01:                         # any number of BSCs per MSC
        mgw: mgw-02                   # optional: a different MGW for this BSC
        bts:
          bts-01:                     # key = a bts instance; order = 'bts N' number in the BSC
            band: DCS1800
            lac: 10
            ci: 101
            bsic: 10
            unit_id: 1001             # IPA unit id: how the BSC recognises this BTS
            trx:
              - { arfcn: 871 }                       # TRX 0 (BCCH carrier)
              - { arfcn: 875, timeslots: [TCH/F, TCH/F, TCH/F, TCH/F, TCH/F, TCH/F, TCH/F, TCH/F] }
```

* One MSC → many BSCs → many BTSs → many TRXs. Nesting makes orphans impossible to write.
* Cell identity (LAC, CI, BSIC, ARFCNs, unit id) belongs to the logical BTS and is rendered into the BSC
  config, because that's where Osmocom keeps it. The BTS process config only holds its unit id, the
  BSC's address and its PHY. That's why a BTS can move between nodes without changing identity.
* For osmo-bts-trx (`variant: trx`), each TRX names its osmo-trx instance and channel:
  `{ arfcn: 871, phy: trx-01, channel: 0 }`. All TRXs of one BTS must use the same osmo-trx.
* Checked: unknown or wrong-type references, BTS in two places, duplicate `bts N`/unit id per BSC,
  duplicate LAC/CI per PLMN, ARFCN outside the band, 8 timeslots per TRX.

## 6. OsmoSTP and signalling

The A interface runs SCCP over M3UA. Every MSC and BSC is an M3UA ASP connecting **to the STP** of its
network (`transport-role client`). The BSC addresses its MSC by point code
(`sccp-address msc-<id> / routing-indicator PC`). The MSC needs no list of its BSCs.

| | `routing: dynamic` (default) | `routing: static` |
|---|---|---|
| STP config | `xua rkm routing-key-allocation dynamic-permitted`, `accept-asp-connections dynamic-permitted` | one `asp` + `as` (routing key = context + point code) + `update route` per MSC/BSC |
| ASP config | client ASP only | client ASP + `as` with the same routing key |
| Needs | nothing | `m3ua_local_port` on every MSC/BSC (the STP tells ASPs apart by remote port) |
| Verified live | yes (all topologies) | yes (single node, LU + SMS) |

* **Point codes** use Osmocom's default 3.8.3 format (`0.23.1`). They must be unique inside one SS7
  domain, meaning all MSCs/BSCs using the same STP. Different STPs are different domains, so two
  independent PLMNs may reuse point codes. Duplicates are an error (`SS7-PC-DUP`).
* Routing contexts are assigned deterministically per STP domain (1, 2, … in declaration order).
* `transport: sctp` on the STP instance switches M3UA from TCP to SCTP for that domain.
* AoIP is what's generated: the BSC and MSC each control their own MGW over MGCP. SCCPlite (IPA-based A
  interface) isn't modelled. Use `config_file`/`raw` for it.

## 7. Endpoints and ports

Every listener and every client socket is explicit in the model. `gsm-lab services` lists them, and
`gsm-lab topology --links` shows each connection and the server endpoint it uses. Nothing assumes
localhost:

* an instance's listeners bind to `instances.<id>.bind`, else its node's `address`;
* clients connect to the server instance's actual endpoint, wherever that runs;
* VTY/CTRL bind to the node's `mgmt_address` (127.0.0.1). If the instance itself binds to a 127.x
  address, VTY/CTRL follow it, so instances separated by loopback addresses need no extra ports.

Default ports (from libosmocore `ports.h` and the programs' VTY code at the pinned versions):

| Service | VTY | CTRL | Other | Configurable? |
|---|---|---|---|---|
| osmo-stp | 4239 | – | M3UA 2905 | all |
| osmo-hlr | 4258 | 4259 | GSUP **4222** | GSUP port **fixed** (only `bind ip`) |
| osmo-mgw | 4243 | 4267 | MGCP 2427/udp, RTP range 4002–16001 | all |
| osmo-msc | 4254 | 4255 | SGs 29118/sctp, MGCP client 2728/udp | all |
| osmo-bsc | 4242 | 4249 | Abis/IP OML **3002**, RSL **3003**, MGCP client 2727/udp | Abis ports **fixed** (only `ipa bind`) |
| osmo-bts | 4241 | 4238 | osmo-bts-trx: TRX client base 5800/udp | all |
| osmo-trx | 4237 | 4236 | base 5700/udp (+2 per channel) | all |
| mobile | 4247 | – | | all |

Override per instance: `vty: 4271`, `ctrl: {ip: 127.0.0.2, port: 4272}`,
`ports: { mgcp: 2430, mgcp_client: 2729 }`.

**Two of a kind on one node.** The validator detects every overlapping port and range, per machine and
protocol. gsm-lab **never renumbers ports by itself**. For the fixed ports (HLR GSUP, BSC Abis) the only
fix is a second IP address: name it on the node (`addresses: { addr2: … }`) and bind the instance to it
(`bind: addr2`, or in a deployment `bsc-02: { node: node-01, bind: addr2 }`). On one machine any
`127.x.y.z` works without configuration.

Before starting, `start` (and `doctor`) checks on the node that the binary exists, bind addresses exist,
ports are free, and kernel SCTP is present (osmo-msc always opens its SGs SCTP listener).

## 8. Installation and dependencies

```
git clone https://github.com/rouhi-dev/gsm.git && cd gsm
./gsm-lab setup --build     # = ./gsm init + access check of every repo + ./gsm doctor + ./gsm build
```

* `./gsm doctor` lists missing host packages; the Debian/Ubuntu list is in `docs/BUILD.md`.
* Everything installs into `./_install` (out-of-tree builds in `./_build`). Nothing needs root.
* **Never configure a component in its source tree** (e.g. running `./configure` inside `apps/osmo-bsc`).
  Autoconf then refuses the out-of-tree build (`source directory already configured`). Fix:
  `make distclean` in that component. It removes only generated, git-ignored files.
* Remote nodes need the same `_install` tree (build there, or copy it to the same path) or a `prefix:` to
  their own install. `gsm-lab doctor` reports missing binaries per node.
* The `gsm-lab` tool itself needs Python ≥ 3.10 and PyYAML (`python3-yaml`) on the control machine only.

## 9. Authentication and submodules

The superproject `github.com/rouhi-dev/gsm` has 12 submodules (forks on GitHub) plus one nested
submodule, `apps/osmo-trx/osmocom-bb` (gitea.osmocom.org, only for `./gsm init --recursive`).

* `./gsm-lab repo-auth` tries every URL (superproject, submodules, nested ones) with
  `GIT_TERMINAL_PROMPT=0` and SSH `BatchMode`, so a missing credential shows up as a failure, never as
  a USER/PASS prompt. It also shows which credential helper git uses per host (name only, never a secret).
* **Current setup on this machine:** all repositories are public, so clone and fetch need no credentials.
  Push to github.com goes through the GitHub CLI helper (`gh auth git-credential`, account `rouhi-dev`,
  token in the system keyring). That one helper covers all 13 GitHub repositories.
* **SSH:** the local key `~/.ssh/id_ed25519` authenticates on GitHub as a *different* account
  (`amrouhi24-hue`). Rewriting the submodule URLs to SSH would therefore push as the wrong identity, so
  gsm-lab doesn't do it. If you want SSH for `rouhi-dev`, add that account's key and a host alias in
  `~/.ssh/config`, then add a global `url.<alias>:.insteadOf https://github.com/` rule.
* **Adding a private GitLab (company) host later:** configure it once per host, not per repo:
  * SSH (preferred): add your key to GitLab, then
    `git config --global url."git@gitlab.example.com:".insteadOf "https://gitlab.example.com/"`.
    This covers every submodule and nested submodule on that host, including future ones.
  * HTTPS: create a personal access token with `read_repository` (and `write_repository` if you push),
    then `git config --global credential.https://gitlab.example.com.helper libsecret` (package
    `git-credential-libsecret`), or `cache`. The first clone asks once; later ones don't. Never put
    tokens into `.gitmodules` or URLs.
  * Then run `./gsm-lab repo-auth` to prove every URL works non-interactively.
* **Updating:** `./gsm-lab update` fast-forwards the superproject (`pull --ff-only`) and then moves **only**
  submodules that are clean and still at the commit the superproject recorded. A submodule with
  uncommitted changes or your own commits is reported as `[SKIP]` and left untouched. Avoid
  `git submodule update --remote` (see the main README). `./gsm-lab repo-status` shows the state.

## 10. Starting, stopping, monitoring

| Command | Effect |
|---|---|
| `start [ids] [--dry-run] [--no-deps] [--backend tmux]` | validate → preflight → render → start in dependency order; waits for each VTY. A failure blocks only that instance's dependants. Named instances pull in their dependencies (unless `--no-deps`). HLR subscribers for all SIMs are provisioned. |
| `stop [ids]` | stops dependants first. Signals a pid only if `/proc/<pid>` still shows the program **and start time** gsm-lab recorded. `mobile` gets SIGINT (clean IMSI detach); the others SIGTERM, then SIGKILL after 5 s. A full stop also removes the lab's tmux session. |
| `restart ids` | stop + start just these |
| `status [ids]` | levels 1–2: process, VTY |
| `health [ids] [--e2e]` | levels 1–3, plus level 4 with `--e2e` (below) |
| `logs id [-f] [-n N]` | the instance's `out.log` (local or remote) |
| `doctor` | validation + nodes + binaries + addresses + free ports + cross-node reachability |
| `provision` | (re)provision HLR subscribers; needed after starting through tmuxinator |

Health levels: (1) the process runs and is still the one gsm-lab started; (2) its VTY answers; (3) its
links are up as the element itself reports them: STP ASPs ACTIVE (one per MSC/BSC of its domain), HLR
GSUP connection per MSC, MSC/BSC MGCP link, BSC `BSSMAP state: CONNECTED`, per BTS OML up and RSL up on
every TRX, mobile `MM idle, normal service`; (4) `--e2e`: each SIM registers, appears in its MSC's VLR,
and an SMS to its own MSISDN goes out through the MSC and comes back to the phone.

A started process is not a working network: `start` reports level 2, `health` reports levels 3–4. Nothing
is restarted automatically. A crash shows up as `exited` with a pointer to `gsm-lab logs`.

**tmux / tmuxinator.** `start --backend tmux` (or `lab: { backend: tmux }`) runs each instance in a pane
of session `gsm-lab-<lab>` on its node (windows `core`, `ran`, `ms`). Tracking and stopping work as in
the default `proc` backend. To keep using tmuxinator:

```
./gsm-lab tmuxinator --write     # writes ~/.config/tmuxinator/gsm-lab-<lab>.yml (refuses to overwrite a non-generated file)
tmuxinator start gsm-lab-<lab>
```

Each pane runs `gsm-lab exec <instance>`, which renders, records state and execs the program. The lab
file stays the only source of truth, and `gsm-lab status/health/stop` work on those processes too. Your
existing `~/.config/tmuxinator/osmocom.yml` is not touched.

## 11. Adding a physical node

1. Make it reachable by key: `ssh-copy-id lab@192.168.50.14`, then `ssh -o BatchMode=yes lab@… true`.
2. Install or build Osmocom there (`./gsm-lab setup --build` in a clone at the same path, or set
   `prefix:`).
3. Add it:
   ```yaml
   nodes:
     node-04: { host: 192.168.50.14, address: 192.168.50.14, ssh: { user: lab } }
   ```
4. `gsm-lab nodes --status`, then `gsm-lab doctor`.

## 12. Moving services between nodes

Change only the placement: `instances.<id>.node`, or the entry in the active deployment. Then:

```
gsm-lab stop bts-03
gsm-lab -d core-bsc validate        # e.g. switch to another named deployment
gsm-lab start bts-03
```

The BTS keeps its LAC/CI/BSIC/unit id/ARFCNs. Its new config points at the same BSC. Moving a *server*
(e.g. the BSC) changes the remote address in its clients' configs; `start` regenerates them, and you
restart the clients (`gsm-lab restart bts-01 bts-02`). Live-tested: the same network in deployments C
and D (`lab/tests/test_live.py`).

## 13. Creating a new PLMN

1. Add the identity: `plmns: { operator_c: { mcc: "001", mnc: "03" } }`.
2. Add instances for its core and RAN: its own MSC (one PLMN per MSC) and its own BSC (one PLMN per
   BSC); preferably its own STP, HLR and MGW too.
3. Add `networks: { net-c: { plmn: operator_c, stp: …, msc: …, bscs: { … } } }`.
4. On a node that already runs another PLMN, bind the new HLR/BSC to another address (fixed ports; see
   section 7). `topologies/multi-plmn.yaml` is a complete, live-tested example.

## 14. A second BSC on an existing MSC

```yaml
instances:
  bsc-02: { service: bsc, node: node-03, point_code: 0.23.4 }   # unique point code in the STP domain
  bts-05: { service: bts, variant: virtual, node: node-03 }
networks:
  net-a:
    bscs:
      bsc-02:
        bts:
          bts-05: { lac: 30, ci: 301, bsic: 30, unit_id: 3001, trx: [{ arfcn: 887 }] }
```

`gsm-lab validate && gsm-lab start bsc-02 bts-05`. The MSC needs no change. The new BSC registers its
ASP at the STP and performs BSSMAP RESET towards the MSC; `gsm-lab health bsc-02` shows it. If bsc-02
shares a node with another BSC, give it its own `vty`/`ctrl` ports and a second address
(`bind: addr2`).

## 15. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `PORT-CONFLICT …` in validate | two instances on one machine use the same IP:port. Give one another port, or (fixed ports) another IP. |
| `preflight: … already in use` | something outside the lab holds the port: `ss -lntup`, or a lab instance started outside gsm-lab. |
| `exited immediately; last output: …` | usually a config the program rejects, or missing hardware (`No UHD devices found`). The last log lines are shown. |
| STP: fewer ASPs active than expected | point code clash, wrong STP address, or (static routing) mismatched `m3ua_local_port`. Check `gsm-lab logs stp-01`. |
| BSC: `A interface: BSSMAP not connected` | MSC not running or not reachable through the STP, or wrong MSC point code. |
| BSC: `OML DOWN` for a BTS | BTS can't reach the BSC's Abis address (remote node + loopback bind is rejected by validate), or `unit_id` mismatch. |
| phone: `PLMN search` | no cell of its home PLMN is up, the virtual-Um multicast groups differ, or the IMSI isn't in the HLR (`gsm-lab provision`). |
| phone rejects SMS: `Memory Exceeded` | phone's `$HOME/.osmocom/bb` missing. gsm-lab creates it; only seen with hand-made setups. |
| `msc exited`: SCTP | `sudo modprobe sctp`: osmo-msc always opens an SCTP SGs listener. |
| `NOT stopped: pid … belongs to another program` | the recorded process is gone and its pid was reused. gsm-lab leaves the other program alone. |
| `hand-edited copy kept as …edited-…` | you edited a generated cfg on the node. Your copy was saved; move the change into the lab file (`raw:`). |
| `./gsm build`: `source directory already configured` | in-tree configure in that component: `make distclean` there (section 8). |

---

## What Osmocom supports

Checked in the pinned sources (Aug-2026 release set) and live where marked:

| Arrangement | Status | Evidence |
|---|---|---|
| One MSC, several BSCs | **Supported** | live: `multi-bsc.yaml` (3 ASPs, 2× BSSMAP CONNECTED) |
| One BSC, several BTSs; one BTS, several TRXs | **Supported** | live: 2 BTS per BSC, BTS with 2 TRX (RSL 2/2) |
| Several PLMNs, each with its own MSC/BSC (same or different nodes) | **Supported by running independent instances** | live: `multi-plmn.yaml`, two PLMNs on one node, LU + SMS in both |
| One MSC serving several PLMNs | **Not supported** (validator: `CAP-MSC-PLMN`) | `osmo-msc` `msc_vty.c`: one `network country code` / `mobile network code` per process |
| One BSC broadcasting several PLMNs (MOCN-like RAN sharing) | **Not supported** (`CAP-BSC-NETS`) | `osmo-bsc` `bts_vty.c` has no per-BTS MCC/MNC; PLMN is network-wide (`bsc_vty.c`) |
| BSC broadcasting PLMN X towards an MSC of PLMN Y | **Special configuration**, rendered, not verified end to end (`PLMN-REWRITE` warning) | `osmo-bsc` `msc N / core-mobile-country-code / core-mobile-network-code` |
| One BSC towards several MSCs (MSC pooling, NRI) | Supported by osmo-bsc, **not modelled** by gsm-lab | see `~/osmocom/msc-pool-lab` |
| One STP for several networks | **Supported**; one SS7 domain: point codes must be unique across them | validator `SHARED-STP` note |
| One HLR for several PLMNs | Works at GSUP level, home/roaming separation **not verified** (`CAP-HLR-SHARED` warning) | |
| One MGW for several networks | Supported (MGCP endpoints are per call) | info note |
| Several instances of a service on one node | **Supported** with distinct ports; HLR (GSUP 4222) and BSC (Abis 3002/3003) **only on distinct IPs** | `hlr_vty.c` `bind ip` only; `e1_input_vty.c` `ipa bind` only |
| Static M3UA routing keys | **Supported** | live: single node, LU + SMS |

## Verification status

| Feature | How verified |
|---|---|
| Build of the pinned set into `_install` | live on this host (`./gsm build`, `./gsm verify --build`, `tests/smoke-core.sh --with-ms`) |
| Repository access check, safe update | live against GitHub; skip-on-local-work in `lab/tests/test_repo.py` |
| Validation rules, config rendering, all example topologies | `lab/tests/test_scenarios.py` (19 tests, the 15 scenarios of the brief) |
| Process lifecycle, ownership check, drift backup, dependency blocking | `lab/tests/test_runtime.py` with real processes |
| Remote (SSH) execution | command construction and every remote script run through an `ssh` shim in `test_runtime.py`; **not** against a real second machine (no sshd here) |
| Single node; 1 MSC + 2 BSCs on 3 nodes; deployments C and D; 2 PLMNs on 1 node; static routing; tmux; tmuxinator | **live** with the real binaries, virtual BTS + virtual phone (`GSM_LAB_LIVE=1 lab/tests/test_live.py`); multi-node runs use one host with one loopback address per node |
| osmo-bts-trx + osmo-trx (SDR) | configs parse and osmo-bts-trx reaches the BSC (OML, both TRXs configured); **RF not tested** (no USRP attached) |
| Real physical BBUs, real phones, voice calls | **not tested** |

## Schema reference

```yaml
lab:          { name, prefix?, run_dir?, backend?: proc|tmux, deployment? }
nodes:        { <id>: { host, address?, addresses?: {name: ip} | [ip], mgmt_address?, ssh?: {user, port, options},
                        prefix?, run_dir?, home?, description? } }
plmns:        { <id>: { mcc: "NNN", mnc: "NN|NNN", mnc_length?, name? } }
instances:
  <id>:
    service: stp | hlr | mgw | msc | bsc | bts | trx | virtphy | mobile
    node: <node>              # or from the deployment
    variant: virtual|trx (bts), uhd|lms|ipc|blade (trx)
    bind: <ip | node address name>
    vty / ctrl: <port> | "ip:port" | { ip, port }
    ports: { <name>: <port> | { ip, port } }      # names: see `gsm-lab services`
    point_code: 0.23.1          # msc, bsc (stp optional)
    m3ua_local_port: 2911       # msc, bsc with routing: static
    transport: tcp | sctp       # stp
    rtp_range: [lo, hi], rtp_bind, endpoints      # mgw
    subscribers: [{ imsi, msisdn, ki }]           # hlr (SIMs of mobiles are added automatically)
    virtual_um: { ms_group, bts_group, ms_port, bts_port, netdev, ttl }   # bts virtual, virtphy
    trx_options: { local_base_port, <osmo-trx 'trx' node command>: value }
    dev_args: "type=b200"       # trx
    virtphy: <id>, sims: [{ imsi, ki, msisdn, imei?, virtphy? }]          # mobile
    raw: |                      # VTY appended verbatim to the generated config
    args: [..]                  # extra command-line arguments
    config_file: /abs/path.cfg  # use a hand-written config instead (validator warns)
    log_level: notice
    enabled: true
networks:     { <id>: { plmn, stp, msc, hlr, mgw, authentication?, encryption?, routing?, short_name?, long_name?,
                        bscs: { <bsc>: { mgw?, ran_plmn?, bts: { <bts>: { band, lac, ci, bsic, unit_id,
                        number?, trx: [{ arfcn, phy?, channel?, nominal_power?, max_power_red?, timeslots? }] } } } } } }
deployments:  { <name>: { <instance>: <node> | { node, bind } } }
x-*:          free-form (for YAML anchors)
```
