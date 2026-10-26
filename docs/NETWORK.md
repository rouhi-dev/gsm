# From sources to a working GSM network

Having all ten submodules checked out, or even built, **does not mean you have an operational
network**. This page covers what the components do, what has been verified, and what's still missing.

## How the pieces connect

```
  Phone (MS)
     │  Um (radio, licensed spectrum!)
  SDR (USRP / LimeSDR / bladeRF)
     │  USB/Ethernet, vendor driver (UHD, LimeSuite, libbladeRF)
  osmo-trx ──TRXC/TRXD (UDP)── osmo-bts-trx
                                    │ Abis/IP (IPA: OML + RSL, TCP 3002/3003)
                               osmo-bsc ──MGCP── osmo-mgw (BSC side) ── RTP
                                    │ A interface: BSSAP / SCCP / M3UA
                               osmo-stp   (from libosmo-sigtran)
                                    │ M3UA
                               osmo-msc ──MGCP── osmo-mgw (MSC side) ── RTP
                                    │ GSUP (TCP 4222)
                               osmo-hlr  (SQLite subscriber DB, auth keys)
```

One osmo-mgw process can serve both BSC and MSC for a lab setup. Production setups usually run one
next to each.

## What's in this workspace and what's verified

| Element | Binary | Built here | Run in smoke test |
|---|---|---|---|
| STP | `osmo-stp` (libosmo-sigtran) | ✔ | ✔ routes M3UA between BSC and MSC |
| HLR | `osmo-hlr` | ✔ | ✔ MSC connected via GSUP |
| MGW | `osmo-mgw` | ✔ | ✔ MGCP link up from MSC and BSC (keepalive) |
| MSC | `osmo-msc` | ✔ | ✔ answers the BSC's BSSMAP RESET |
| BSC | `osmo-bsc` | ✔ | ✔ A-interface CONNECTED; OML + RSL from the BTS |
| BTS | `osmo-bts-virtual` | ✔ | ✔ Abis/IP to the BSC (no radio; virtual Um) |
| BTS for SDR | `osmo-bts-trx` | ✔ | ✘ needs osmo-trx + hardware |
| TRX | `osmo-trx-uhd` / `-lms` / `-blade` / `-ipc` | ✔ (driver per flags) | ✘ needs SDR hardware |

`tests/smoke-core.sh` produces the ✔ entries in the last column. It needs a kernel with SCTP support,
because osmo-msc always opens an SCTP listener for its SGs (LTE CS-fallback) interface. Everything else
in the test runs M3UA over TCP.

**Not verified by anything in this repo:** radio transmission, a phone camping on the cell, location
update/authentication, calls, SMS, handover. Those need the items below.

## What a real network additionally needs

**Required, not software in this repo:**

1. **SDR hardware supported by osmo-trx** (e.g. Ettus B200/B210, LimeSDR, bladeRF 2.0), with a stable
   reference clock (GPSDO or a good TCXO; GSM is sensitive to frequency error). Or a BTS with an
   integrated PHY (sysmoBTS etc.), which uses a different osmo-bts backend.
2. **The SDR vendor driver** (`libuhd-dev`, `limesuite-dev`, `libbladerf-dev`) and the matching
   `--with-*` flag for osmo-trx (`config/build.local.conf`).
3. **Programmable SIM cards** and their Ki/OPc values provisioned in osmo-hlr. Phones won't attach
   without authentication unless you deliberately configure the network to accept that. [pySim](https://gitea.osmocom.org/sim-card/pysim)
   is the usual tool for writing SIMs.
4. **A licence for the frequencies, or a shielded RF enclosure.** Transmitting on GSM bands without
   authorisation is illegal almost everywhere and can interfere with real networks and emergency calls.
   Use a test licence, a Faraday cage, or cabled connections with attenuators.
5. **Configuration** for each element (MCC/MNC, LAC, ARFCN, point codes, IPs). The `tests/smoke/`
   configs are loopback-only starting points, and each component ships examples in `doc/examples/`.
   Osmocom's manuals are at <https://downloads.osmocom.org/docs/>.

**Optional Osmocom components, deliberately not included** (add them as submodules if you need them):

| Need | Repositories | Notes |
|---|---|---|
| GPRS / EDGE data | osmo-pcu, osmo-sgsn, osmo-ggsn (+ libgtpnl) | osmo-pcu attaches to osmo-bts. osmo-sgsn also needs a dependency chain of its own. |
| Calls to/from outside (SIP) | osmo-sip-connector + a PBX (Asterisk/FreeSWITCH) | needs osmo-msc with `mncc external` |
| SMS via SMPP (ESME) | libsmpp34 | rebuild osmo-msc with `--enable-smpp` |
| 3G (IuCS/IuPS) | osmo-iuh, libasn1c, osmo-hnbgw | rebuild osmo-msc with `--enable-iu` |
| Virtual phone (no radio) | osmocom-bb (`virtphy`, `mobile`) | pairs with osmo-bts-virtual for end-to-end tests without RF |
| Protocol test suites | osmo-ttcn3-hacks, docker-playground | Osmocom's own integration tests |
| Ready-made dev workspace | osmo-dev | Osmocom's own make-based multi-repo build. Worth comparing with this repo. |

All of these have mirrors at `github.com/osmocom/<name>` (checked October 2026) and official repos on
gitea.osmocom.org.

## Suggested next steps

1. Create the forks (`tools/create-forks.sh`), push this repo, and let CI build it once.
2. Run `./gsm build --check` and `tests/smoke-core.sh` on your target host (with `modprobe sctp`).
3. For a test without RF: build osmocom-bb's `virtphy` + `mobile` and attach a virtual phone to
   `osmo-bts-virtual`. That exercises location update, auth, SMS and calls end to end.
4. With hardware: set `CONFIGURE_FLAGS[osmo-trx]` for your SDR, build, and work through the OsmoBTS
   and OsmoTRX manuals inside a shielded setup.
5. Before relying on modified elements, run the relevant TTCN-3 suites.
