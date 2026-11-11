"""Run commands on a node: locally, or over SSH with the same shell snippets.

Remote access uses the system ssh client in BatchMode (keys / agent / ~/.ssh/config); gsm-lab never
handles passwords. Every remote action is a small bash script whose text is visible with --verbose.
"""

import shlex
import subprocess
import time


class NodeError(Exception):
    pass


VERBOSE = False


class NodeExec:
    def __init__(self, node):
        self.node = node

    def _cmd(self, script):
        if self.node.local:
            return ["bash", "-c", script]
        target = f"{self.node.ssh_user}@{self.node.host}" if self.node.ssh_user else self.node.host
        return (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-p", str(self.node.ssh_port)]
                + list(self.node.ssh_options) + [target, "bash -c " + shlex.quote(script)])

    def run(self, script, input=None, timeout=30, check=False):
        if VERBOSE:
            print(f"--- [{self.node.id}] $ {script}")
        try:
            p = subprocess.run(self._cmd(script), input=input, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise NodeError(f"{self.node.id}: command timed out after {timeout}s") from None
        except FileNotFoundError as e:
            raise NodeError(f"{self.node.id}: {e}") from None
        if not self.node.local and p.returncode == 255:
            raise NodeError(f"{self.node.id}: ssh to {self.node.host} failed: {p.stderr.strip()[-300:]}")
        if check and p.returncode != 0:
            raise NodeError(f"{self.node.id}: command failed ({p.returncode}): {p.stderr.strip()[-500:]}")
        return p.returncode, p.stdout, p.stderr

    # ------------------------------------------------------------ files
    def read(self, path):
        rc, out, _ = self.run(f"cat {shlex.quote(path)} 2>/dev/null")
        return out if rc == 0 else None

    def write(self, path, text, mode=None):
        q = shlex.quote(path)
        script = f"mkdir -p $(dirname {q}) && cat > {q}.tmp.$$ && mv -f {q}.tmp.$$ {q}"
        if mode:
            script += f" && chmod {mode} {q}"
        self.run(script, input=text, check=True)

    # ------------------------------------------------------------ probes
    def probe(self, binaries=()):
        """One round trip: addresses, listening sockets, binaries, SCTP, python3, tmux."""
        bins = " ".join(shlex.quote(b) for b in binaries)
        script = r"""
echo "##addr"; ip -o addr show 2>/dev/null | awk '{split($4,a,"/"); print $2, a[1]}'
echo "##listen"; ss -Hlntu 2>/dev/null | awk '{print $1, $5}'
echo "##sctp"; ss -Hln --sctp 2>/dev/null | awk '{print "sctp", $4}'
echo "##bins"; for b in BINS; do if [ -x "$b" ]; then echo "ok $b"; else echo "missing $b"; fi; done
echo "##tools"; for t in python3 tmux setsid ss ip; do command -v $t >/dev/null && echo "ok $t" || echo "missing $t"; done
echo "##kernel"; if [ -e /proc/net/sctp ] || grep -qw sctp /proc/modules 2>/dev/null; then echo "sctp yes"; else echo "sctp no"; fi
echo "##host"; uname -srm; echo "$HOME"
""".replace("BINS", bins)
        rc, out, err = self.run(script, timeout=20)
        res = {"addr": [], "listen": [], "bins": {}, "tools": {}, "sctp": False, "uname": "", "home": ""}
        sec = None
        for line in out.splitlines():
            if line.startswith("##"):
                sec = line[2:]
                continue
            parts = line.split()
            if not parts:
                continue
            if sec == "addr" and len(parts) == 2:
                res["addr"].append(parts[1])
            elif sec in ("listen", "sctp") and len(parts) == 2:
                proto = parts[0]
                hostport = parts[1]
                host, _, port = hostport.rpartition(":")
                host = host.strip("[]").split("%")[0]
                if port.isdigit():
                    res["listen"].append((proto, "0.0.0.0" if host in ("*", "0.0.0.0", "::") else host, int(port)))
            elif sec == "bins" and len(parts) == 2:
                res["bins"][parts[1]] = parts[0] == "ok"
            elif sec == "tools" and len(parts) == 2:
                res["tools"][parts[1]] = parts[0] == "ok"
            elif sec == "kernel":
                res["sctp"] = parts[-1] == "yes"
            elif sec == "host":
                if not res["uname"]:
                    res["uname"] = line
                else:
                    res["home"] = line
        return res

    def tcp_reachable(self, ip, port, timeout=2):
        """Is ip:port accepting TCP connections, as seen from this node?"""
        rc, _, _ = self.run(f"timeout {timeout} bash -c 'exec 3<>/dev/tcp/{ip}/{port}' 2>/dev/null", timeout=timeout + 10)
        return rc == 0

    def vty(self, port, commands, ip="127.0.0.1", timeout=3):
        """Run VTY commands on this node (python3 on the node), return the text output or None."""
        rc, out, _ = self.run("python3 -c " + shlex.quote(VTY_CLIENT) + " " + " ".join(
            shlex.quote(x) for x in [ip, str(port), str(timeout)] + list(commands)), timeout=timeout * (len(commands) + 3) + 10)
        return out if rc == 0 else None


# Minimal VTY client (same protocol handling as tests/smoke-core.sh): waits for the prompt after each command.
VTY_CLIENT = r"""
import re, socket, sys, time
ip, port, to, cmds = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), ["enable"] + sys.argv[4:]
try:
    s = socket.create_connection((ip, port), timeout=to)
except OSError:
    sys.exit(2)
def until_prompt():
    buf, end = b"", time.time() + to
    while time.time() < end:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        if re.search(rb"[>#] $", buf):
            return buf, True
    return buf, False
until_prompt()
for c in cmds:
    s.sendall(c.encode() + b"\r\n")
    buf, ok = until_prompt()
    sys.stdout.write(re.sub(r"[^\t\n\x20-\x7e]", "", buf.decode("latin-1")))
    if not ok:     # no prompt: the answer is incomplete and later answers would be misattributed
        sys.stdout.flush()
        sys.exit(3)
s.close()
"""


def executors(lab):
    return {nid: NodeExec(n) for nid, n in lab.nodes.items()}


def ping_node(ex):
    t = time.time()
    try:
        rc, out, err = ex.run("echo ok", timeout=10)
    except NodeError as e:
        return False, str(e)
    return rc == 0 and out.strip() == "ok", f"{(time.time() - t) * 1000:.0f} ms"
