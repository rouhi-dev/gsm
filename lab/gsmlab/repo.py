"""Phase 0: clone / update / authentication checks for the gsm superproject and all its submodules.

Builds on ./gsm (init, doctor, build, status), which already knows the pins and the build order.
This module adds:
  * an authentication check of every submodule URL (direct and nested) without ever prompting:
    GIT_TERMINAL_PROMPT=0 + ssh BatchMode, so a missing credential shows up as a clear failure instead
    of a USER/PASS prompt per submodule;
  * which credential mechanism git will use per host (helper name only; no secret is ever printed);
  * a safe update: fast-forward the superproject, then move only submodules that are clean and still at
    the commit the superproject recorded, so local work in a submodule is never checked out away.
"""

import os
import subprocess

from .model import REPO_ROOT

ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=8"}


def git(*args, cwd=REPO_ROOT, timeout=60):
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=ENV)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def submodules(root=REPO_ROOT, prefix=""):
    """(path, url, initialised) for all submodules, nested ones included (recursing into checked-out ones)."""
    rc, out, _ = git("config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url)$", cwd=root)
    if rc != 0:
        return []
    paths, urls = {}, {}
    for line in out.splitlines():
        key, val = line.split(" ", 1)
        name = key[len("submodule."):].rsplit(".", 1)[0]
        (paths if key.endswith(".path") else urls)[name] = val
    res = []
    for name, path in paths.items():
        # local override (./gsm init --source mirror|gitea) wins over .gitmodules, as in git itself
        rc, local_url, _ = git("config", f"submodule.{name}.url", cwd=root)
        url = local_url if rc == 0 and local_url else urls.get(name, "")
        full = os.path.join(root, path)
        init = os.path.exists(os.path.join(full, ".git"))
        res.append((os.path.join(prefix, path), url, init))
        if init and os.path.exists(os.path.join(full, ".gitmodules")):
            res += submodules(full, os.path.join(prefix, path))
    return res


def host_of(url):
    if url.startswith("git@"):
        return "ssh://" + url[4:].split(":", 1)[0]
    if "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://{rest.split('/', 1)[0].split('@')[-1]}"
    return url


def auth_report(out=print):
    """Check that every repository URL can be reached non-interactively. -> number of failures."""
    rc, origin, _ = git("remote", "get-url", "origin")
    urls = [(".", origin)] + [(p, u) for p, u, _ in submodules()]
    hosts = {}
    fails = 0
    out("== Repository access (non-interactive: no prompts)")
    for path, url in urls:
        rc, _, err = git("ls-remote", "--exit-code", url, "HEAD", timeout=60)
        h = host_of(url)
        hosts.setdefault(h, []).append(path)
        if rc == 0:
            out(f"[ OK ] {path:28} {url}")
        else:
            fails += 1
            out(f"[FAIL] {path:28} {url}")
            msg = err.splitlines()[-1] if err else "no output"
            out(f"       -> {msg}")
            if "terminal prompts disabled" in err or "could not read Username" in err:
                out("       -> needs credentials: see `gsm-lab setup --help` / lab/docs/LAB.md 'Authentication'")
    out("\n== Credential mechanism per host (helper names only)")
    for h, paths in hosts.items():
        if h.startswith("ssh://"):
            out(f"  {h:32} SSH key (agent / ~/.ssh/config)   [{len(paths)} repo(s)]")
            continue
        rc, helper, _ = git("config", "--get-urlmatch", "credential.helper", h + "/")
        helper = helper.splitlines()[-1] if helper else ""
        shown = helper if helper else "none (anonymous: fine for public repos; push will prompt)"
        out(f"  {h:32} {shown}   [{len(paths)} repo(s)]")
    return fails


def gsm(*args, timeout=None):
    p = subprocess.run([os.path.join(REPO_ROOT, "gsm"), *args], cwd=REPO_ROOT, env=ENV, timeout=timeout)
    return p.returncode


def setup(recursive=False, build=False, out=print):
    rc = gsm("init", *(["--recursive"] if recursive else []))
    fails = auth_report(out)
    rc2 = gsm("doctor")
    rc3 = gsm("build") if build and rc == 0 and rc2 == 0 else 0
    return 1 if (rc or fails or rc2 or rc3) else 0


def update(out=print, root=None, show_status=True):
    """Fast-forward the superproject, then update only submodules that are safe to move."""
    root = root or REPO_ROOT
    before = {}
    for path, _, init in submodules(root):
        if not init:
            continue
        full = os.path.join(root, path)
        _, head, _ = git("rev-parse", "HEAD", cwd=full)
        _, dirty, _ = git("status", "--porcelain", "--untracked-files=no", cwd=full)
        parent = os.path.dirname(full)
        while not os.path.exists(os.path.join(parent, ".gitmodules")) and parent != root:
            parent = os.path.dirname(parent)
        rel = os.path.relpath(full, parent)
        _, ls, _ = git("ls-tree", "HEAD", rel, cwd=parent)
        recorded = ls.split()[2] if ls else ""
        before[path] = (head, recorded, bool(dirty), parent, rel)

    out("== Superproject: git pull --ff-only")
    rc, so, se = git("pull", "--ff-only", cwd=root, timeout=300)
    out(so or se)
    if rc != 0:
        out("[FAIL] superproject not updated (not a fast-forward, or local changes conflict). Nothing else changed.")
        return 1
    fails = 0
    out("\n== Submodules")
    for path, (head, recorded, dirty, parent, rel) in before.items():
        if dirty:
            out(f"[SKIP] {path}: has uncommitted changes (left as is)")
            continue
        if recorded and head != recorded:
            out(f"[SKIP] {path}: checked out at {head[:12]}, not at the recorded {recorded[:12]} (your work; left as is)")
            continue
        _, new_ls, _ = git("ls-tree", "HEAD", rel, cwd=parent)
        new = new_ls.split()[2] if new_ls else ""
        if new == head:
            out(f"[ OK ] {path}: unchanged ({head[:12]})")
            continue
        rc, so, se = git("submodule", "update", "--init", "--", rel, cwd=parent, timeout=600)
        if rc == 0:
            out(f"[ OK ] {path}: {head[:12]} -> {new[:12]}")
        else:
            fails += 1
            out(f"[FAIL] {path}: {se.splitlines()[-1] if se else 'update failed'}")
    if show_status:
        gsm("status")
    return 1 if fails else 0


def status(out=print):
    _, branch, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    _, ab, _ = git("rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    _, dirty, _ = git("status", "--porcelain")
    out(f"superproject {REPO_ROOT}: branch {branch}" + (f", behind/ahead upstream: {ab.replace(chr(9), '/')}" if ab else ""))
    for line in dirty.splitlines():
        out(f"  {line}")
    return gsm("status")
