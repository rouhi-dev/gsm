#!/usr/bin/env bash
# Core-network smoke test: starts osmo-stp, osmo-hlr, osmo-mgw, osmo-msc, osmo-bsc and osmo-bts-virtual
# on 127.0.0.1 (configs in tests/smoke/), then checks via VTY that the elements have connected:
#
#   BSC/MSC <-M3UA/TCP-> STP   both ASPs ACTIVE on the STP
#   BSC/MSC  -> MGW            MGCP client link confirmed by keepalive
#   BSC  -> MSC                A-interface: BSSMAP RESET acknowledged (MSC link "connected")
#   MSC  -> HLR                GSUP client connected
#   BTS  -> BSC                Abis/IP OML + RSL links up
#
# What it does NOT show: radio, a phone attaching, calls or SMS. See docs/NETWORK.md.
#
#   tests/smoke-core.sh [--keep]     (--keep leaves the processes running for manual inspection)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$ROOT/tests/smoke"
PREFIX="${GSM_PREFIX:-$ROOT/_install}"
KEEP=0; [[ "${1:-}" == --keep ]] && KEEP=1
WORK="$(mktemp -d "${TMPDIR:-/tmp}/gsm-smoke.XXXXXX")"
FAILS=0
ok()   { printf '[ OK ] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*"; FAILS=$((FAILS+1)); }

export PATH="$PREFIX/bin:$PATH" LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# ---- preflight
for b in osmo-stp osmo-hlr osmo-mgw osmo-msc osmo-bsc osmo-bts-virtual; do
	command -v "$b" >/dev/null || { fail "$b not found in $PREFIX/bin -- run ./gsm build"; }
done
(( FAILS )) && exit 1
# osmo-msc always opens an SCTP listener (SGs interface), so the kernel must support SCTP.
if ! python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM, 132)' 2>/dev/null &&
   [[ "${GSM_SMOKE_ALLOW_NO_SCTP:-0}" != 1 ]]; then
	fail "kernel SCTP not available (osmo-msc needs it for its SGs listener)"
	echo "       -> sudo modprobe sctp   (containers: the host kernel must provide it)"
	exit 1
fi
for port in 2905 4222 2427 3002 3003 4239 4258 4243 4254 4242 4241; do
	if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then fail "port $port already in use on 127.0.0.1"; fi
done
(( FAILS )) && { echo "       -> stop the running Osmocom processes first"; exit 1; }

# ---- start (dependency order), each in $WORK with its own log
declare -A PIDS
start() { # name binary args...
	local n="$1"; shift
	( cd "$WORK" && exec "$@" >"$WORK/$n.log" 2>&1 ) & PIDS[$n]=$!
	sleep 0.5
}
cleanup() {
	(( KEEP )) && { echo "processes left running (--keep); logs in $WORK"; return; }
	kill "${PIDS[@]}" 2>/dev/null; wait 2>/dev/null
	(( FAILS )) && echo "logs kept in $WORK" || rm -rf "$WORK"
}
trap cleanup EXIT

start stp osmo-stp -c "$CFG/osmo-stp.cfg"
start hlr osmo-hlr -c "$CFG/osmo-hlr.cfg" -l "$WORK/hlr.db"
start mgw osmo-mgw -c "$CFG/osmo-mgw.cfg"
start msc osmo-msc -c "$CFG/osmo-msc.cfg"
start bsc osmo-bsc -c "$CFG/osmo-bsc.cfg"
start bts osmo-bts-virtual -c "$CFG/osmo-bts-virtual.cfg"

# ---- VTY helper: vty <port> <command...>  -> prints the output (waits for each prompt)
vty() {
	python3 - "$@" <<'PY'
import socket, sys, time, re
port, cmds = int(sys.argv[1]), ["enable"] + sys.argv[2:]
s = socket.create_connection(("127.0.0.1", port), timeout=3)
def until_prompt():
    buf, end = b"", time.time() + 3
    while time.time() < end:
        try: chunk = s.recv(65536)
        except socket.timeout: break
        if not chunk: break
        buf += chunk
        if re.search(rb"[>#] $", buf): break
    return buf
until_prompt()
for c in cmds:
    s.sendall(c.encode() + b"\r\n")
    sys.stdout.write(re.sub(r"[^\t\n\x20-\x7e]", "", until_prompt().decode("latin-1")))
s.close()
PY
}
# wait_for <timeout-s> <description> <port> <regex> <vty command...>
wait_for() {
	local t="$1" desc="$2" port="$3" re="$4"; shift 4
	local out i
	for ((i = 0; i < t; i++)); do
		out="$(vty "$port" "$@" 2>/dev/null)"
		grep -Ezq "$re" <<<"$out" && { ok "$desc"; return 0; }   # -z: regex may span lines
		sleep 1
	done
	fail "$desc (no match for /$re/ after ${t}s)"; printf '%s\n' "$out" | tail -n 12 | sed 's/^/       | /'
	return 1
}

echo "Started; waiting for links (work dir $WORK)"
for n in stp hlr mgw msc bsc bts; do
	kill -0 "${PIDS[$n]}" 2>/dev/null || { fail "osmo-$n exited at startup"; tail -n 5 "$WORK/$n.log" | sed 's/^/       | /'; }
done

wait_for 20 "STP: MSC and BSC ASPs active (M3UA)"     4239 'ASP_ACTIVE.*ASP_ACTIVE' "show cs7 instance 0 asp"
wait_for 20 "HLR: MSC connected (GSUP)"               4258 "'[^']*MSC' from"       "show gsup-connections"
wait_for 30 "MSC: MGCP link to MGW up (keepalive)"    4254 'MGCP link: +connected,UP' "show mgw-pool"
wait_for 30 "BSC: MGCP link to MGW up (keepalive)"    4242 'MGCP link: +connected,UP' "show mgw-pool"
wait_for 30 "BSC: A-interface to MSC up (BSSMAP RESET acknowledged)" 4242 'BSSMAP state: CONNECTED' "show mscs"
wait_for 30 "BSC: BTS 0 Abis/IP OML connected"        4242 'OML Link state: connected' "show bts 0"
wait_for 30 "BSC: BTS 0 RSL up on its TRX"            4242 'where RSL is up: +1' "show bts 0"

for n in stp hlr mgw msc bsc bts; do
	kill -0 "${PIDS[$n]}" 2>/dev/null || fail "osmo-$n is no longer running"
done
echo
if (( FAILS )); then echo "Result: $FAILS failure(s)."; exit 1; fi
echo "Result: OK -- core elements started and interconnected. (No radio, no MS: see docs/NETWORK.md.)"
