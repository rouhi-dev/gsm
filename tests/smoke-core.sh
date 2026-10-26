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
# With --with-ms it also starts a virtual phone from osmocom-bb (virtphy + mobile, test SIM) on the
# virtual Um interface of osmo-bts-virtual (UDP multicast, no radio) and checks, from the network side:
#
#   MS   -> MSC/HLR            location update completed, with authentication REQUIRED (COMP128v1,
#                              Ki provisioned in osmo-hlr) -- the MSC fetched auth tuples from the HLR
#   MS   -> MS                 an SMS sent to its own MSISDN is submitted (MO), delivered back (MT)
#                              and stored by the phone
#
# What it does NOT show: real radio, real phones, voice calls. See docs/NETWORK.md.
#
#   tests/smoke-core.sh [--with-ms] [--keep]   (--keep leaves the processes running for inspection)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="$ROOT/tests/smoke"
PREFIX="${GSM_PREFIX:-$ROOT/_install}"
KEEP=0; MS=0
for a in "$@"; do case "$a" in --keep) KEEP=1;; --with-ms) MS=1;;
	*) echo "usage: $0 [--with-ms] [--keep]" >&2; exit 2;; esac; done
WORK="$(mktemp -d "${TMPDIR:-/tmp}/gsm-smoke.XXXXXX")"
FAILS=0
ok()   { printf '[ OK ] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*"; FAILS=$((FAILS+1)); }

export PATH="$PREFIX/bin:$PREFIX/sbin:$PATH" LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# ---- preflight
for b in osmo-stp osmo-hlr osmo-mgw osmo-msc osmo-bsc osmo-bts-virtual; do
	command -v "$b" >/dev/null || { fail "$b not found in $PREFIX/bin -- run ./gsm build"; }
done
if (( MS )); then for b in virtphy mobile; do
	command -v "$b" >/dev/null || fail "$b not found in $PREFIX/bin -- run ./gsm build osmocom-bb"; done; fi
(( FAILS )) && exit 1
# osmo-msc always opens an SCTP listener (SGs interface), so the kernel must support SCTP.
if ! python3 -c 'import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM, 132)' 2>/dev/null &&
   [[ "${GSM_SMOKE_ALLOW_NO_SCTP:-0}" != 1 ]]; then
	fail "kernel SCTP not available (osmo-msc needs it for its SGs listener)"
	echo "       -> sudo modprobe sctp   (containers: the host kernel must provide it)"
	exit 1
fi
for port in 2905 4222 2427 3002 3003 4239 4258 4243 4254 4242 4241 4247; do
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

# counter <port> <name>: value of one rate counter (0 if absent)
counter() { vty "$1" "show rate-counters" 2>/dev/null | awk -v n="$2:" '$1==n {print $2; f=1; exit} END {if(!f) print 0}'; }
# wait_counter <timeout-s> <description> <port> <name> <minimum>
wait_counter() {
	local t="$1" desc="$2" port="$3" name="$4" min="$5" v=0 i
	for ((i = 0; i < t; i++)); do v="$(counter "$port" "$name")"; (( v >= min )) && { ok "$desc ($name=$v)"; return 0; }; sleep 1; done
	fail "$desc ($name=$v after ${t}s, expected >= $min)"; return 1
}

if (( MS && ! FAILS )); then
	echo; echo "Virtual phone (osmocom-bb virtphy + mobile) on the virtual Um interface"
	IMSI=001010000000101; MSISDN=5101
	KI="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"; KI_SPACED="$(sed 's/../& /g; s/ $//' <<<"$KI")"
	out="$(vty 4258 "subscriber imsi $IMSI create" "subscriber imsi $IMSI update msisdn $MSISDN" \
		"subscriber imsi $IMSI update aud2g comp128v1 ki $KI" "subscriber imsi $IMSI show")"
	grep -q "KI=$KI" <<<"$out" && ok "HLR: subscriber $IMSI provisioned (MSISDN $MSISDN, COMP128v1, random Ki)" ||
		{ fail "HLR: could not provision subscriber"; printf '%s\n' "$out" | tail -n 6 | sed 's/^/       | /'; }
	mkdir -p "$WORK/home/.osmocom/bb"
	sed "s|@WORK@|$WORK|g; s|@IMSI@|$IMSI|; s|@KI_SPACED@|$KI_SPACED|" "$CFG/mobile.cfg.in" >"$WORK/mobile.cfg"
	start virtphy virtphy -s "$WORK/l2"
	sleep 1
	( cd "$WORK" && HOME="$WORK/home" exec mobile -c "$WORK/mobile.cfg" >"$WORK/mobile.log" 2>&1 ) & PIDS[mobile]=$!
	wait_for 60 "MS: camped and registered (MM idle, normal service)" 4247 'MM idle, normal service' "show ms 1"
	wait_counter 10 "MSC: fetched auth tuples from the HLR (authentication required)" 4254 'gsup:rx:send_auth_info:res' 1
	wait_counter 10 "MSC: location update completed"                 4254 'loc_update_resp:completed' 1
	wait_for 10 "MSC: subscriber attached in the VLR"                4254 "IMSI: $IMSI" "show subscriber cache"
	TEXT="gsm-smoke-$RANDOM$RANDOM"
	vty 4247 "sms 1 $MSISDN $TEXT" >/dev/null
	wait_counter 30 "MSC: SMS submitted by the MS (MO)"              4254 'sms:submitted' 1
	for ((i = 0; i < 30; i++)); do grep -q "$TEXT" "$WORK/home/.osmocom/bb/sms.txt" 2>/dev/null && break; sleep 1; done
	grep -q "$TEXT" "$WORK/home/.osmocom/bb/sms.txt" 2>/dev/null && ok "MS: SMS delivered back (MT) and stored by the phone" ||
		fail "MS: SMS '$TEXT' not received by the phone within 30s"
fi

for n in "${!PIDS[@]}"; do
	kill -0 "${PIDS[$n]}" 2>/dev/null || fail "$n is no longer running"
done
echo
if (( FAILS )); then echo "Result: $FAILS failure(s)."; exit 1; fi
if (( MS )); then echo "Result: OK -- core interconnected; a virtual phone authenticated, registered and exchanged an SMS. (No real radio: see docs/NETWORK.md.)"
else echo "Result: OK -- core elements started and interconnected. (No radio, no MS: see docs/NETWORK.md; try --with-ms.)"; fi
