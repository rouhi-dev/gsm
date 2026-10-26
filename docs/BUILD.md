# Building

## Dependency graph

Build-time requirements at the pinned releases (from each `configure.ac`):

| Component | Needs (in this workspace) | Installs |
|---|---|---|
| libosmocore | – | libosmocore, libosmogsm, libosmovty, libosmoctrl, libosmocodec, libosmocoding, libosmogb, … |
| libosmo-netif | libosmocore | libosmo-netif |
| libosmo-abis | libosmocore, libosmo-netif | libosmoabis, libosmotrau |
| libosmo-sigtran | libosmocore, libosmo-netif | libosmo-sigtran, **osmo-stp** |
| libosmo-gprs | libosmocore | libosmo-gprs-{rlcmac,llc,sndcp,gmm,sm}, libosmo-csn1 |
| osmo-mgw | libosmocore, libosmo-netif, libosmo-abis | osmo-mgw, libosmo-mgcp-client |
| osmo-hlr | libosmocore, libosmo-abis | osmo-hlr, libosmo-gsup-client, libosmo-mslookup |
| osmo-msc | libosmocore, netif, abis, sigtran, **osmo-mgw** (mgcp-client), **osmo-hlr** (gsup-client) | osmo-msc |
| osmo-bsc | libosmocore, netif, abis, sigtran, **osmo-mgw** (mgcp-client) | osmo-bsc, abisip-find, ipaccess-config, … |
| osmo-bts | libosmocore, libosmo-netif, libosmo-abis (+trau) | osmo-bts-virtual, osmo-bts-trx, … |
| osmo-trx | libosmocore | osmo-trx-uhd / -lms / -blade / -ipc |
| osmocom-bb | libosmocore, libosmo-gprs | virtphy, mobile, trxcon, ccch_scan, bcch_scan, cell_log, gprsdecode, … in `bin/`; osmocon, osmoload in `sbin/` |

osmocom-bb isn't one autotools project but five (`src/host/{osmocon,virt_phy,trxcon,gprsdecode,layer23}`).
The fifth column of `components.conf` lists them, and `./gsm build` builds each in turn, the same as
upstream's `contrib/jenkins.sh`. Its bundled `src/shared/libosmocore` is for the phone firmware only and is
ignored: the host programs link against the workspace's libosmocore. GAPK and Lua support in layer23 are
optional and off unless their libraries are installed.

So two applications are also build dependencies: osmo-msc needs osmo-mgw and osmo-hlr installed
first, and osmo-bsc needs osmo-mgw.

`components.conf` lists components in a valid build order, and `./gsm build` follows it.

## Host packages (Debian 12 / Ubuntu 24.04)

```bash
sudo apt install build-essential autoconf automake libtool pkg-config git \
    libtalloc-dev libsctp-dev libsqlite3-dev sqlite3 \
    libgnutls28-dev libmnl-dev libpcsclite-dev libusb-1.0-0-dev liburing-dev \
    libortp-dev libfftw3-dev \
    libuhd-dev uhd-host libboost-all-dev  # SDR driver for osmo-trx (UHD needs boost), pick per hardware
```

Required in every configuration: **talloc, lksctp (libsctp), sqlite3**. The rest depends on configure flags.
`./gsm doctor` reads `config/build.conf` (+ `build.local.conf`) and tells you exactly what's missing.

Prefer not to build? Osmocom publishes binary packages (Debian/Ubuntu, "latest" and "nightly" feeds) via
its OBS repositories. See <https://osmocom.org/projects/cellular-infrastructure/wiki/Binary_Packages>. This
workspace is for when you want sources you control and can patch.

## Configure flags

Defaults live in `config/build.conf`. For machine-specific changes create `config/build.local.conf`
(git-ignored), for example:

```bash
CONFIGURE_FLAGS[osmo-trx]="--with-lms"                                  # LimeSDR instead of UHD
CONFIGURE_FLAGS[libosmocore]="--disable-pcsc --disable-libusb --disable-uring"
CONFIGURE_FLAGS[libosmo-abis]="--disable-dahdi --disable-ortp"
CONFIGURE_FLAGS[osmo-bts]="--enable-trx --disable-ortp"
```

Notable choices in the defaults:

- `osmo-bts --enable-trx`: builds `osmo-bts-trx` (for osmo-trx/SDR). `osmo-bts-virtual` is always built.
  Proprietary-PHY backends (sysmoBTS, LiteCell 1.5, OC-2G, Octasic) need vendor headers and are off.
- `osmo-trx --with-uhd`: Ettus USRP. Alternatives: `--with-lms`, `--with-bladerf`, `--with-ipc`.
  Multi-ARFCN support is on by default and needs fftw3f (`--disable-multi-arfcn` drops it).
- `libosmo-abis --disable-dahdi`: DAHDI is only for E1/T1 cards (classic Abis). Abis/IP doesn't need it.
- osmo-msc's SMPP (`--enable-smpp`, needs libsmpp34) and 3G (`--enable-iu`, needs osmo-iuh + libasn1c)
  stay off, matching upstream defaults. Those repos aren't part of this workspace.

## How `./gsm build` works

For each component, in order: `autoreconf -fi` in the source tree → `configure --prefix=$GSM_PREFIX`
in `_build/<name>/` (out-of-tree) → `make -jN` → (`make check` with `--check`) → `make install`. Output goes
to `_build/logs/<name>.log`. On failure it prints the failed step, the first error lines and the log tail,
then stops, because later components may depend on the failed one.

- Default prefix: `./_install`. Change it with `GSM_PREFIX=/opt/osmocom ./gsm build`. A system prefix such
  as `/usr/local` needs `sudo` for `make install`; a user-owned prefix is simpler.
- Only some components: `./gsm build osmo-bsc osmo-msc` (they're still built in manifest order). The
  libraries they need must already be installed in the prefix.
- Your own environment is passed through. `PKG_CONFIG_PATH`, `CPPFLAGS` and `LDFLAGS` work as usual,
  e.g. for dependencies installed in a non-standard prefix.
- systemd unit files (`osmo-*.service`) are installed to `$GSM_PREFIX/lib/systemd/system`, not the system
  directory. Upstream's default would write to `/usr/lib/systemd/system` regardless of `--prefix`. To use
  them, copy them into place deliberately (adjust `ExecStart` paths). To override, set
  `--with-systemdsystemunitdir=…` in `config/build.local.conf`.
- Every program is linked with `-rpath <prefix>/lib`, so it loads the workspace's libraries even outside
  `./gsm env`. Some (osmocom-bb's `mobile`, `virtphy`) aren't linked through libtool and would otherwise
  pick up an older system libosmocore that has the same library file name.
- `./gsm env` also puts `_install/sbin` on `PATH` (osmocon, osmoload).
- Generated autotools files are git-ignored upstream, so `./gsm status` stays clean after a build.

Building one component by hand (same as the tool does):

```bash
eval "$(./gsm env)"
cd libs/libosmocore && autoreconf -fi && mkdir -p ../../_build/libosmocore && cd ../../_build/libosmocore
../../libs/libosmocore/configure --prefix="$PWD/../../_install" && make -j"$(nproc)" && make install
```

## Tests

| Level | How | What it shows |
|---|---|---|
| Unit tests | `./gsm build --check` (runs `make check` per component) | the code behaves as upstream expects on your host |
| Workspace checks | `./gsm verify --build` | pins = releases, history complete, version requirements consistent, binaries installed |
| Core smoke test | `tests/smoke-core.sh` | STP, HLR, MGW, MSC and BSC start, and the BSC↔MSC A-interface comes up over M3UA |
| Virtual phone | `tests/smoke-core.sh --with-ms` | additionally: an osmocom-bb virtual phone authenticates (COMP128v1, auth *required*), registers, and gets its own SMS back |
| Full integration | Osmocom's TTCN-3 suites ([osmo-ttcn3-hacks](https://gitea.osmocom.org/ttcn3/osmo-ttcn3-hacks), run via [docker-playground](https://gitea.osmocom.org/osmocom/docker-playground)) | protocol conformance of each element against simulated peers |

Some unit tests depend on the host. libosmocore's SCTP and io_uring tests can be turned off with
`--disable-sctp-tests --disable-uring-tests`. Two tests need **IPv6 loopback (`::1`)** and have no
switch: libosmocore's `socket` and osmo-mgw's `mgcp_client` (which crashes rather than failing cleanly when
`::1` is missing). Both fail on hosts or containers with IPv6 disabled. osmo-hlr's `db_upgrade` test
needs the `sqlite3` command-line tool (`apt install sqlite3`). `./gsm doctor` warns about all three.
If those are your only failures, it's the environment, not the code. `./gsm build <name>` (without
`--check`) still installs the component.

The TTCN-3 suites are Osmocom's real integration tests. They're heavyweight (Eclipse Titan + Docker),
so they aren't wired into this repo. Use them before running modified elements in anything that matters.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[FAIL] <name>: needs <module> (from <component>), which is not installed` | a dependency was not built/installed. Run the `./gsm build …` command printed after the failure |
| `[FAIL] <name>: <module> would come from /usr/..., not from …/_install` | a dependency is missing from the prefix and a system package would be used. Build the named component first |
| "can't find the binaries" | they're in `_install/bin`. The build must finish (`Result: OK`); `eval "$(./gsm env)"` puts them on `PATH` |
| `[FAIL] <name>: cannot reach https://github.com/rouhi-dev/<name>.git` | fork missing → `tools/create-forks.sh`, or `./gsm init --source mirror` |
| `not our ref` / `reference is not a tree` during init | pinned commit not in your fork → push the tag (docs/UPSTREAM.md) |
| `No package 'talloc' found` (or another module) | install the `-dev` package `./gsm doctor` names, or disable the feature |
| `netinet/sctp.h not found` | `apt install libsctp-dev`. If it's in a custom prefix, export `CPPFLAGS=-I<prefix>/include LDFLAGS=-L<prefix>/lib` |
| an app fails to link against a fresh library | rebuild everything downstream of a library you changed: `./gsm build` |
| `install-systemdsystemunitDATA` error during install | fixed in `./gsm` (units go into the prefix). `git pull` and re-run `./gsm build` |
| binaries can't find `libosmo*.so` at runtime | `eval "$(./gsm env)"` (sets `LD_LIBRARY_PATH`) |
