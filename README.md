# gsm: an Osmocom GSM workspace

This is the central entry point for a selected set of [Osmocom](https://osmocom.org) cellular-infrastructure
projects. Each component is a **Git submodule** that points to a native GitHub fork under
[`rouhi-dev`](https://github.com/rouhi-dev) and is **pinned to one exact upstream release commit**. Nothing is
copied or rewritten. Every component keeps its own full history, licence, and upstream relationship.

> **Status:** these are pinned, build-compatible sources for a 2G (GSM) circuit-switched network: voice,
> SMS, USSD. This repo alone does **not** give you a working network. That also needs radio hardware,
> configuration, SIM cards and a licence to transmit. See [docs/NETWORK.md](docs/NETWORK.md).

## Layout

```
gsm/
├── gsm                     workspace tool: init / status / verify / doctor / build / env / bump
├── components.conf         manifest: name, path, pinned release, official upstream (build order)
├── config/build.conf       default ./configure flags per component (override in build.local.conf)
├── libs/                   shared libraries
│   ├── libosmocore         core utilities, GSM protocol codecs, VTY, CTRL, logging
│   ├── libosmo-netif       stream/datagram sockets, RTP, IPA, SCTP helpers
│   ├── libosmo-abis        Abis interface (A-bis over IP / E1), TRAU frames
│   └── libosmo-sigtran     SCCP, M3UA, SUA, and the osmo-stp signalling transfer point
├── apps/                   network elements
│   ├── osmo-mgw            media gateway (RTP / Osmux / E1 TRAU), used by both BSC and MSC
│   ├── osmo-hlr            home location register, SQLite subscriber DB, GSUP server
│   ├── osmo-msc            mobile switching centre, VLR, SMSC, USSD
│   ├── osmo-bsc            base station controller
│   ├── osmo-bts            base transceiver station (osmo-bts-trx, osmo-bts-virtual, ...)
│   └── osmo-trx            SDR transceiver for osmo-bts-trx (UHD, LimeSuite, bladeRF, IPC)
├── tools/create-forks.sh   create/check the GitHub forks (safe, idempotent)
├── tests/smoke-core.sh     start the core network and check that the elements interconnect
├── .github/workflows/      CI: init → build → verify → smoke test on Ubuntu
└── docs/                   BUILD.md · UPSTREAM.md · NETWORK.md
```

`libs/` and `apps/` follow the dependency boundary: nothing in `libs/` depends on anything in `apps/`.

## Pinned versions

All ten pins come from Osmocom's coordinated **August 2026 release** (tags dated 2026-08-20 / 21). Every
component's `configure.ac` minimum versions are satisfied by the other pins; `./gsm verify` re-checks
this from the sources on every run.

| Component | Release | Commit | Requires (Osmocom) |
|---|---|---|---|
| libosmocore | 1.14.2 | `73fed749b1e0` | – |
| libosmo-netif | 1.8.0 | `2ce2cac140d0` | libosmocore ≥ 1.12.0 |
| libosmo-abis | 2.2.0 | `1f6508c5e489` | libosmocore ≥ 1.14.2, netif ≥ 1.8.0 |
| libosmo-sigtran | 2.3.0 | `121209dfbb98` | libosmocore ≥ 1.14.2, netif ≥ 1.8.0 |
| osmo-mgw | 1.16.1 | `bc18cad31e3e` | libosmocore ≥ 1.14.2, netif ≥ 1.8.0, abis ≥ 2.2.0 |
| osmo-hlr | 1.9.4 | `856b5d67e6d8` | libosmocore ≥ 1.11.0, abis ≥ 2.0.0 |
| osmo-msc | 1.16.0 | `1c80b962ee74` | core ≥ 1.14.2, abis ≥ 2.2.0, netif ≥ 1.8.0, sigtran ≥ 2.3.0, mgcp-client ≥ 1.16.0, gsup-client ≥ 1.9.0 |
| osmo-bsc | 1.15.0 | `1a2627f61f6a` | core ≥ 1.14.2, abis ≥ 2.2.0, netif ≥ 1.8.0, sigtran ≥ 2.3.0, mgcp-client ≥ 1.16.0 |
| osmo-bts | 1.12.0 | `e8dac6c91a88` | core ≥ 1.14.2, abis/trau ≥ 2.2.0, netif ≥ 1.8.0 |
| osmo-trx | 1.8.0 | `80c54268fc87` | libosmocore ≥ 1.10.0 |

Build order (= the order in `components.conf`):
`libosmocore → libosmo-netif → libosmo-abis → libosmo-sigtran → osmo-mgw → osmo-hlr → osmo-msc → osmo-bsc → osmo-bts → osmo-trx`

## Quick start

```bash
# 1. clone (pick one)
git clone https://github.com/rouhi-dev/gsm.git && cd gsm && ./gsm init     # recommended
git clone --recurse-submodules https://github.com/rouhi-dev/gsm.git       # plain git also works*

# 2. check host prerequisites (Debian/Ubuntu package list in docs/BUILD.md)
./gsm doctor

# 3. build everything into ./_install (out-of-tree builds in ./_build, logs in ./_build/logs)
./gsm build                 # or: ./gsm build --check   (also runs each project's unit tests)

# 4. verify
./gsm verify --build        # pins == release tags, full history, version compat, binaries present
tests/smoke-core.sh         # optional: start STP/HLR/MGW/MSC/BSC and check they interconnect
eval "$(./gsm env)"         # put _install/bin and its libraries on PATH / LD_LIBRARY_PATH
```

\* `--recurse-submodules` also fetches osmo-trx's own nested submodule (`osmocom-bb`, large, only needed
for `--with-mstrx`) from gitea.osmocom.org. `./gsm init` skips it unless you pass `--recursive`.

`./gsm init` checks out each component on its own and reports per component, so one unreachable
repo doesn't hide the rest. It adds two extra remotes to every submodule: `upstream` (the official
gitea.osmocom.org repo) and `mirror` (github.com/osmocom). It is idempotent, so you can re-run it any time.

If your forks don't exist yet, or you only want to build: `./gsm init --source mirror` (github.com/osmocom)
or `--source gitea` (official). This sets a local, uncommitted URL override. The pinned commits are the
same either way.

## Commands

| Command | What it does |
|---|---|
| `./gsm init [--source fork\|mirror\|gitea] [--recursive]` | check out all submodules at their pins; add `upstream` / `mirror` remotes |
| `./gsm status` | one line per component: pin, what is checked out, local modifications, origin |
| `./gsm verify [--build]` | manifest ↔ .gitmodules, gitlink = release tag, non-shallow history, licence file, `configure.ac` version requirements vs. the other pins, build products |
| `./gsm doctor` | host tools and system libraries required by the current configure flags |
| `./gsm build [--check] [-j N] [component…]` | autoreconf → configure → make (→ make check) → make install, in dependency order; stops at the first failure and shows the relevant log lines |
| `./gsm env` | prints `export` lines for `_install/` |
| `./gsm bump <component> <tag\|commit> [--push]` | move one pin; refuses if the commit is missing from your fork; stages the change for you to review and commit |

Every command prints `[ OK ]` / `[WARN]` / `[FAIL]` lines and exits non-zero on failure.

## Updating safely

- `git pull && git submodule update` (or `./gsm init`) moves your checkouts to the commits the gsm repo
  records. That's reproducible: everybody gets the same trees.
- **Avoid `git submodule update --remote`.** It jumps every component to its current branch tip, which
  breaks the coordinated release set. Change pins one at a time with `./gsm bump`, run `./gsm verify` and
  `./gsm build`, then commit.
- Before any bump, make sure the target commit is in your fork. GitHub's "Sync fork" button only updates
  the default branch, **not tags**. See [docs/UPSTREAM.md](docs/UPSTREAM.md).

## Limitations

- This verifies sources, versions and builds. It doesn't show that phones can attach or call. That needs
  RF hardware and SIMs ([docs/NETWORK.md](docs/NETWORK.md)).
- Scope is 2G circuit-switched only. GPRS/EDGE (osmo-pcu, osmo-sgsn, osmo-ggsn), SMPP (libsmpp34), SIP
  interconnect (osmo-sip-connector) and 3G (osmo-iuh, libasn1c) aren't included. They're easy to add as
  more submodules later.
- The forks are copies taken at fork time. Upstream development continues at gitea.osmocom.org and is
  reviewed on gerrit.osmocom.org, so patches meant for upstream go to Gerrit, not to GitHub pull requests.
- Linux only (this is Osmocom's supported platform). The tool needs bash ≥ 4.

## Documentation

- [docs/BUILD.md](docs/BUILD.md): system packages, configure flags, building single components, tests, troubleshooting
- [docs/UPSTREAM.md](docs/UPSTREAM.md): official hosting vs. GitHub mirrors, the fork model, syncing, bumping, contributing
- [docs/NETWORK.md](docs/NETWORK.md): what a working GSM network needs beyond this repo, missing components, next steps

## Licences

Each submodule keeps its own licence and attribution, untouched, in its own repository. Per the
top-level `COPYING` files at the pinned releases:

- **GPL-2.0:** libosmocore, libosmo-netif, libosmo-sigtran
- **AGPL-3.0:** libosmo-abis, osmo-mgw, osmo-hlr, osmo-msc, osmo-bsc, osmo-bts, osmo-trx

Individual source files carry their own licence headers (mostly `GPL-2.0+` in libosmocore, with a
few LGPL/GPL-3.0+ files), and those take precedence. The AGPL matters if you offer modified network elements as a service: you
have to make the source of your modifications available.

The tooling and docs in this repo don't have a licence yet. Add a `LICENSE` file if you want others
to reuse them.
