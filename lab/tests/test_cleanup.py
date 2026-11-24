"""`gsm-lab cleanup`: discovery, classification and removal, in a throwaway sandbox (never the real system).

The sandbox has a home with GSM/Osmocom repositories next to unrelated ones, out-of-tree builds, gsm-lab state and
history backups; a /usr/local-like prefix where real ELF programs and libraries (built with gcc) of the GSM stack sit
next to unrelated files, a library of another Osmocom project, a file owned by the package manager and a symbolic link
to a file outside; a dedicated prefix; and a temp directory. Each test checks the plan and then, file by file, that
--apply removed exactly what the plan said and nothing else.
"""

import hashlib
import io
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

import helpers  # noqa: F401  (sys.path)
from gsmlab import cleanup

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow",
       "GIT_CONFIG_VALUE_0": "always"}
HAVE_GCC = shutil.which("gcc") is not None and shutil.which("ar") is not None


def w(path, text="", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)
    return path


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout


def gcc(*args):
    subprocess.run(["gcc", *args], check=True, capture_output=True)


def snapshot(root):
    """path -> (type, content hash or link target) for everything below root (links not followed)."""
    out = {}
    for d, dirs, files in os.walk(root, followlinks=False):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[p] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[p] = ("dir", "")
            else:
                out[p] = ("file", hashlib.sha256(open(p, "rb").read()).hexdigest())
    return out


def gone_under(before, after, path):
    return all(p not in after for p in before if p == path or p.startswith(path + "/"))


def intact_under(before, after, path):
    return all(after.get(p) == v for p, v in before.items() if p == path or p.startswith(path + "/"))


class Sandbox:
    """Builds the whole fixture once per test (fast enough: a few gcc runs and small git repositories)."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="gsm-cleanup-test.")
        R = self.root
        self.home, self.pfx, self.opt = f"{R}/home", f"{R}/usrlocal", f"{R}/opt/osmocom"
        self.tmp, self.dpkg, self.outside = f"{R}/tmp", f"{R}/dpkg-info", f"{R}/outside"
        for d in (self.home, self.pfx, self.tmp, self.dpkg, self.outside):
            os.makedirs(d)
        self.build_binaries()
        self.build_repos()
        self.build_prefix()
        self.build_rest()

    # ----- ELF files
    def build_binaries(self):
        b = f"{self.root}/elf"
        os.makedirs(b)
        w(f"{b}/lib.c", "int osmo_init(void) { return 0; }\n")
        w(f"{b}/prog.c", "#include <unistd.h>\nint osmo_init(void);\n"
                         "int main(int c, char **v) { if (c > 1) sleep(60); return osmo_init(); }\n")
        w(f"{b}/plain.c", "int main(void) { return 0; }\n")
        gcc("-shared", "-fPIC", "-Wl,-soname,libosmocore.so.22", "-o", f"{b}/libosmocore.so.22.4.2", f"{b}/lib.c")
        gcc("-shared", "-fPIC", "-Wl,-soname,libosmogsm.so.20", "-o", f"{b}/libosmogsm.so.20.5.0", f"{b}/lib.c")
        gcc("-shared", "-fPIC", "-Wl,-soname,libosmo-fl2k.so.0", "-o", f"{b}/libosmo-fl2k.so.0.1", f"{b}/lib.c")
        gcc("-shared", "-fPIC", "-Wl,-soname,libfoo.so.1", "-o", f"{b}/libfoo.so.1.0", f"{b}/lib.c")
        os.symlink("libosmocore.so.22.4.2", f"{b}/libosmocore.so")
        os.symlink("libosmocore.so.22.4.2", f"{b}/libosmocore.so.22")   # what the programs load (rpath)
        gcc("-o", f"{b}/osmo-prog", f"{b}/prog.c", f"-L{b}", "-losmocore", f"-Wl,-rpath,{b}")
        gcc("-o", f"{b}/plain", f"{b}/plain.c")
        gcc("-c", "-fPIC", "-o", f"{b}/lib.o", f"{b}/lib.c")
        subprocess.run(["ar", "rcs", f"{b}/libosmocore.a", f"{b}/lib.o"], check=True)
        self.elf = b

    # ----- repositories
    def repo(self, path, remote=None, files=None, push=True):
        os.makedirs(path, exist_ok=True)
        g(path, "init", "-q", "-b", "master")
        for rel, text in (files or {"README": "x\n"}).items():
            w(os.path.join(path, rel), text)
        g(path, "add", "-A")
        g(path, "commit", "-q", "-m", "init")
        if remote:
            g(path, "remote", "add", "origin", remote)
            if push:      # what a clone of an up-to-date remote looks like
                g(path, "update-ref", "refs/remotes/origin/master", "HEAD")
        return path

    def build_repos(self):
        H = self.home
        osmo_ac = "AC_INIT([osmo-bsc], [1.15.0])\nPKG_CHECK_MODULES(LIBOSMOCORE, libosmocore >= 1.14)\n"
        self.gsm = self.repo(f"{H}/dev/gsm", files={
            "gsm": "#!/bin/bash\n", "components.conf": "libosmocore libs/libosmocore 1 osmocom/libosmocore\n"
            "osmocom-bb apps/osmocom-bb x phone-side/osmocom-bb\n", "lab/gsmlab/__init__.py": "",
            ".gitignore": "/_build/\n/_install/\n",
            "libs/libosmocore/src/Makefile.am": "lib_LTLIBRARIES = libosmocore.la\nbin_PROGRAMS = osmo-arfcn \\\n"
                                                "\tosmo-auc-gen\nnoinst_PROGRAMS = helper\n",
            "libs/libosmocore/libosmocore.pc.in": "Name: Osmocom Core Library\n",
            "libs/libosmocore/include/osmocom/core/msgb.h": "/* msgb */\n",
            "libs/libosmocore/contrib/systemd/osmo-bsc.service": "[Service]\n",
            "apps/osmocom-bb/src/host/layer23/src/mobile/Makefile.am": "bin_PROGRAMS = mobile\n"})
        bare = f"{self.root}/remotes/gsm.git"
        g(self.root, "clone", "-q", "--bare", self.gsm, bare)
        g(self.gsm, "remote", "add", "origin", bare)
        g(self.gsm, "fetch", "-q", "origin")
        w(f"{self.gsm}/_build/libosmocore/config.status", self.cfgstatus(f"{self.gsm}/_build/libosmocore",
                                                                         f"{self.gsm}/libs/libosmocore",
                                                                         f"{self.gsm}/_install"))
        shutil.copy(f"{self.elf}/osmo-prog", w(f"{self.gsm}/_install/bin/osmo-bsc"))
        self.clone2 = f"{H}/elsewhere/gsm-copy"
        g(self.root, "clone", "-q", bare, self.clone2)
        self.libosmocore = self.repo(f"{H}/osmocom/libosmocore", "https://gitea.osmocom.org/osmocom/libosmocore")
        self.dirty = self.repo(f"{H}/osmocom/osmo-msc", "https://gitea.osmocom.org/cellular-infrastructure/osmo-msc")
        w(f"{self.dirty}/new-work.c", "x\n")
        self.unpushed = self.repo(f"{H}/osmocom/osmo-hlr", "https://gitea.osmocom.org/cellular-infrastructure/osmo-hlr")
        w(f"{self.unpushed}/f", "y\n")
        g(self.unpushed, "commit", "-qam", "local", "--allow-empty")
        g(self.unpushed, "add", "f")
        g(self.unpushed, "commit", "-qm", "unpushed")
        self.backed = self.repo(f"{H}/osmocom/osmo-bts", "https://gitea.osmocom.org/cellular-infrastructure/osmo-bts")
        g(self.backed, "update-ref", "refs/backup/attribution-cleanup/master", "HEAD")
        self.worktree = f"{H}/wt/osmo-bts-wt"
        g(self.backed, "worktree", "add", "-q", self.worktree, "-b", "wt")
        g(self.worktree, "update-ref", "refs/remotes/origin/wt", "HEAD")
        self.fork = self.repo(f"{H}/fork/osmo-bsc", "git@github.com:someone/osmo-bsc.git",
                              files={"configure.ac": osmo_ac, "src/Makefile.am": "bin_PROGRAMS = osmo-bsc\n"})
        self.unrelated = self.repo(f"{H}/work/webapp", "https://github.com/acme/webapp.git")
        self.lookalike = self.repo(f"{H}/work/osmo-thing", "https://github.com/bob/osmo-thing.git",
                                   files={"configure.ac": "AC_INIT([osmo-thing], [1])\n"})
        self.export = f"{H}/export/osmo-bsc-src"
        w(f"{self.export}/configure.ac", osmo_ac)
        w(f"{self.export}/.tarball-version", "1.15.0\n")

    @staticmethod
    def cfgstatus(builddir, srcdir, prefix):
        return (f"#! /bin/sh\nac_cs_config=\"'--prefix={prefix}'\"\nac_pwd='{builddir}'\nsrcdir='{srcdir}'\n")

    # ----- the /usr/local-like prefix
    def build_prefix(self):
        P, E = self.pfx, self.elf
        os.makedirs(f"{P}/bin")
        os.makedirs(f"{P}/lib/pkgconfig")
        for n in ("osmo-bsc", "mobile", "osmo-arfcn"):
            shutil.copy(f"{E}/osmo-prog", f"{P}/bin/{n}")
        shutil.copy(f"{E}/plain", f"{P}/bin/modem")                    # generic name, links nothing of Osmocom
        shutil.copy(f"{E}/plain", f"{P}/bin/osmo-notreally")            # osmo- name, links nothing of Osmocom
        w(f"{P}/bin/osmo-release.sh", "#!/bin/sh\n# osmocom release helper\n", 0o755)
        shutil.copy(f"{E}/osmo-prog", f"{self.outside}/precious-osmo-bin")
        os.symlink(f"{self.outside}/precious-osmo-bin", f"{P}/bin/osmo-linked")
        for f in ("libosmocore.so.22.4.2", "libosmogsm.so.20.5.0", "libosmo-fl2k.so.0.1", "libfoo.so.1.0"):
            shutil.copy(f"{E}/{f}", f"{P}/lib/{f}")
        os.symlink("libosmocore.so.22.4.2", f"{P}/lib/libosmocore.so.22")
        os.symlink("libosmocore.so.22.4.2", f"{P}/lib/libosmocore.so")
        shutil.copy(f"{E}/libosmocore.a", f"{P}/lib/libosmocore.a")
        w(f"{P}/lib/libosmocore.la", "# libosmocore.la - a libtool library file\ndlname='libosmocore.so.22'\n")
        w(f"{P}/lib/pkgconfig/libosmocore.pc", "Name: Osmocom Core Library\nURL: https://osmocom.org\n"
                                               "Libs: -L${libdir} -losmocore\n")
        w(f"{P}/lib/pkgconfig/zlib.pc", "Name: zlib\n")
        w(f"{P}/include/osmocom/core/msgb.h", "/* msgb */\n")
        w(f"{P}/include/osmocom/core/generated.h", "/* generated */\n")
        w(f"{P}/include/zlib.h", "/* zlib */\n")
        w(f"{P}/etc/osmocom/osmo-bsc.cfg", "! cfg\n")
        w(f"{P}/etc/other.conf", "x\n")
        w(f"{P}/share/osmocom/osmo-release.mk", "x\n")
        w(f"{P}/share/doc/osmo-bsc/examples/osmo-bsc.cfg", "! cfg\n")
        w(f"{P}/share/doc/zlib/README", "zlib\n")
        w(f"{P}/lib/systemd/system/osmo-bsc.service", f"[Service]\nExecStart={P}/bin/osmo-bsc -c x\n")
        w(f"{P}/lib/systemd/system/other.service", "[Service]\nExecStart=/usr/bin/other\n")
        # owned by the package manager: never removed, whatever it looks like
        w(f"{self.dpkg}/libosmogsm20.list", f"{P}/lib/libosmogsm.so.20.5.0\n")
        # the dedicated prefix of an out-of-tree build (configure --prefix=<opt>)
        shutil.copy(f"{E}/osmo-prog", w(f"{self.opt}/bin/osmo-bsc"))
        shutil.copy(f"{E}/libosmocore.so.22.4.2", w(f"{self.opt}/lib/libosmocore.so.22.4.2"))

    def build_rest(self):
        H = self.home
        self.build = f"{H}/builds/osmo-bsc-build"
        w(f"{self.build}/config.status", self.cfgstatus(self.build, self.fork, self.opt))
        w(f"{self.build}/src/osmo-bsc.o", "obj")
        self.other_build = f"{H}/builds/webapp-build"
        w(f"{self.other_build}/config.status", self.cfgstatus(self.other_build, self.unrelated, "/usr/local"))
        self.backup = f"{H}/dev/history-backup-20261124"
        w(f"{self.backup}/gsm.bundle", "bundle\n")
        w(f"{H}/.local/state/gsm-lab/ledger/t.json", "{}\n")
        w(f"{H}/.local/state/gsm-lab/t/hlr/hlr.db", "db\n")
        w(f"{H}/.config/tmuxinator/gsm-lab-t.yml", "# GENERATED by `gsm-lab tmuxinator` from x -- do not edit\n")
        w(f"{H}/.config/tmuxinator/osmocom.yml", "name: osmocom\nwindows:\n  - osmo-hlr -c x\n")
        w(f"{H}/.config/tmuxinator/web.yml", "name: web\n")
        os.makedirs(f"{self.tmp}/bsc-aworker-snap.Ab12")
        os.makedirs(f"{self.tmp}/aworker_proc.Xy9z")
        w(f"{self.tmp}/unrelated.txt", "x\n")

    def scanner(self, **kw):
        return cleanup.Scanner(home=self.home, roots=[self.home], prefixes=[self.pfx], temp_dirs=[self.tmp],
                               default_prefixes=False, dpkg_info=self.dpkg, **kw)

    def cleanup(self):
        for d, dirs, files in os.walk(self.root):
            for n in dirs:
                os.chmod(os.path.join(d, n), 0o755)
        shutil.rmtree(self.root, ignore_errors=True)


def plan(items):
    return {i.path: i for i in items}


@unittest.skipUnless(HAVE_GCC, "needs gcc and ar to build ELF fixtures")
class Cleanup(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.cleanup()

    def scan(self, **kw):
        with redirect_stdout(io.StringIO()):
            sc = self.s.scanner(**kw)
            return sc, plan(sc.scan())

    def apply(self, items):
        with redirect_stdout(io.StringIO()):
            return cleanup.apply(list(items.values()), log=lambda *_: None)

    def assertAction(self, p, path, action, why=None):
        self.assertIn(path, p, f"{path} not in the plan")
        self.assertEqual(p[path].action, action, f"{path}: {p[path].action} ({p[path].reason})")
        if why:
            self.assertIn(why, p[path].reason)

    # ------------------------------------------------------------------ classification
    def test_plan_default(self):
        s, P = self.s, self.s.pfx
        sc, p = self.scan()
        # installed GSM/Osmocom files: ELF evidence, names from the sources, namespaces
        for f in ("bin/osmo-bsc", "bin/mobile", "bin/osmo-arfcn", "bin/osmo-release.sh", "bin/osmo-linked",
                  "lib/libosmocore.so.22.4.2", "lib/libosmocore.so.22", "lib/libosmocore.so", "lib/libosmocore.a",
                  "lib/libosmocore.la", "lib/pkgconfig/libosmocore.pc", "include/osmocom/core/msgb.h",
                  "include/osmocom/core/generated.h", "etc/osmocom/osmo-bsc.cfg", "share/osmocom/osmo-release.mk",
                  "share/doc/osmo-bsc/examples/osmo-bsc.cfg", "lib/systemd/system/osmo-bsc.service"):
            self.assertAction(p, f"{P}/{f}", "remove")
        self.assertIn("ELF links libosmocore.so.22", p[f"{P}/bin/mobile"].evidence[0])
        self.assertIn("name from the project sources", p[f"{P}/bin/mobile"].evidence[1])
        # unrelated software in the same prefix: not even listed, or preserved with a reason
        for f in ("bin/modem", "lib/libfoo.so.1.0", "lib/libosmo-fl2k.so.0.1", "lib/pkgconfig/zlib.pc",
                  "include/zlib.h", "etc/other.conf", "share/doc/zlib/README", "lib/systemd/system/other.service"):
            self.assertTrue(f"{P}/{f}" not in p or p[f"{P}/{f}"].action != "remove", f)
        self.assertAction(p, f"{P}/bin/osmo-notreally", "preserve", "does not link")
        self.assertAction(p, f"{P}/lib/libosmogsm.so.20.5.0", "preserve", "package manager")
        # whole directories
        self.assertAction(p, s.opt, "remove", "dedicated prefix")
        self.assertAction(p, s.build, "remove", "out-of-tree build")
        self.assertAction(p, s.other_build, "preserve", "not a verified")
        self.assertAction(p, f"{s.gsm}/_build", "remove")
        self.assertAction(p, f"{s.gsm}/_install", "remove")
        self.assertAction(p, f"{s.home}/.local/state/gsm-lab", "remove")
        self.assertAction(p, f"{s.home}/.config/tmuxinator/gsm-lab-t.yml", "remove")
        self.assertAction(p, f"{s.home}/.config/tmuxinator/osmocom.yml", "preserve", "hand-written")
        self.assertNotIn(f"{s.home}/.config/tmuxinator/web.yml", p)
        self.assertAction(p, f"{s.tmp}/bsc-aworker-snap.Ab12", "remove")
        self.assertAction(p, f"{s.tmp}/aworker_proc.Xy9z", "remove")
        self.assertNotIn(f"{s.tmp}/unrelated.txt", p)
        # repositories: kept without --repo, protected with backup refs, unrelated ones not listed
        for r in (s.gsm, s.clone2, s.libosmocore, s.fork, s.worktree, s.dirty, s.unpushed):
            self.assertAction(p, r, "preserve", "kept without --repo")
        self.assertAction(p, s.backed, "protected", "backup refs")
        self.assertNotIn(s.unrelated, p)
        self.assertAction(p, s.lookalike, "preserve", "not a verified")
        self.assertAction(p, s.export, "preserve", "kept without --repo")
        self.assertFalse(any(path.startswith(s.backup) for path in p), "history backup listed")

    def test_dry_run_changes_nothing(self):
        before = snapshot(self.s.root)
        out = io.StringIO()
        with redirect_stdout(out):
            self.s.scanner().scan()
        self.assertEqual(snapshot(self.s.root), before)

    # ------------------------------------------------------------------ apply
    def test_apply_removes_exactly_the_plan(self):
        s = self.s
        before = snapshot(s.root)
        sc, p = self.scan()
        removed, skipped = self.apply(p)
        after = snapshot(s.root)
        self.assertEqual(skipped, [], [(i.path, why) for i, why in skipped])
        planned = [i.path for i in p.values() if i.action == "remove"]
        for path in planned:
            self.assertTrue(gone_under(before, after, path), f"{path} still there")
        # everything else is byte-identical (removed directories only lose what is inside them)
        for path, v in before.items():
            if any(path == q or path.startswith(q + "/") for q in planned):
                continue
            if v[0] == "dir" and path not in after:
                self.fail(f"directory {path} removed although not planned")
            if v[0] != "dir":
                self.assertEqual(after.get(path), v, f"{path} changed although not planned")
        # the symbolic link went, its target outside the prefix did not
        self.assertNotIn(f"{s.pfx}/bin/osmo-linked", after)
        self.assertIn(f"{s.outside}/precious-osmo-bin", after)
        # namespace directories went once empty, standard ones stay
        self.assertNotIn(f"{s.pfx}/include/osmocom", after)
        self.assertIn(f"{s.pfx}/include", after)
        self.assertIn(f"{s.pfx}/lib/pkgconfig", after)
        self.assertTrue(intact_under(before, after, s.backup))
        self.assertTrue(intact_under(before, after, s.unrelated))
        self.assertTrue(intact_under(before, after, s.libosmocore))       # repositories without --repo

    def test_repo_mode(self):
        s = self.s
        before = snapshot(s.root)
        sc, p = self.scan(with_repo=True)
        for r in (s.gsm, s.clone2, s.libosmocore, s.fork, s.worktree, s.export):
            self.assertAction(p, r, "remove")
        self.assertAction(p, s.dirty, "preserve", "uncommitted")
        self.assertAction(p, s.unpushed, "preserve", "no remote")
        self.assertAction(p, s.backed, "protected")
        self.assertEqual(p[s.worktree].kind, "worktree")
        self.assertTrue(p[f"{s.gsm}/_build"].extra.get("with_parent"))
        removed, skipped = self.apply(p)
        self.assertEqual(skipped, [], [(i.path, why) for i, why in skipped])
        after = snapshot(s.root)
        for r in (s.gsm, s.clone2, s.libosmocore, s.fork, s.worktree, s.export):
            self.assertTrue(gone_under(before, after, r), r)
        for r in (s.dirty, s.unpushed, s.backed, s.unrelated, s.lookalike, s.backup):
            self.assertTrue(intact_under(before, after, r) or r == s.backed, r)
        # the protected repository keeps its refs; its removed worktree was pruned
        self.assertIn("refs/backup/attribution-cleanup/master", g(s.backed, "for-each-ref", "--format=%(refname)"))
        self.assertNotIn(s.worktree, g(s.backed, "worktree", "list"))

    def test_discard_local_work(self):
        sc, p = self.scan(with_repo=True, discard_local_work=True)
        self.assertAction(p, self.s.dirty, "remove")
        self.assertAction(p, self.s.unpushed, "remove")
        self.assertAction(p, self.s.backed, "protected")

    def test_keep_repo_builds(self):
        sc, p = self.scan(keep_repo_builds=True)
        self.assertAction(p, f"{self.s.gsm}/_install", "preserve")

    # ------------------------------------------------------------------ safety
    def test_library_kept_for_a_program_that_stays(self):
        s = self.s
        shutil.copy(f"{s.elf}/osmo-prog", f"{s.pfx}/bin/thirdparty-tool")   # links libosmocore, unknown name
        sc, p = self.scan()
        self.assertAction(p, f"{s.pfx}/bin/thirdparty-tool", "preserve", "not one the projects install")
        self.assertAction(p, f"{s.pfx}/lib/libosmocore.so.22.4.2", "preserve", "still needed by")
        self.assertAction(p, f"{s.pfx}/bin/osmo-bsc", "remove")

    def test_running_program_is_kept(self):
        s = self.s
        proc = subprocess.Popen([f"{s.pfx}/bin/osmo-arfcn", "sleep"], env={**ENV, "LD_LIBRARY_PATH": s.elf})
        try:
            for _ in range(50):
                if os.path.realpath(f"/proc/{proc.pid}/exe") == f"{s.pfx}/bin/osmo-arfcn":
                    break
                time.sleep(0.05)
            self.assertIsNone(proc.poll(), "the fixture program did not keep running")
            sc, p = self.scan()
            self.assertAction(p, f"{s.pfx}/bin/osmo-arfcn", "preserve", "running process")
        finally:
            proc.send_signal(signal.SIGKILL)
            proc.wait()

    def test_changed_after_scan_is_skipped(self):
        s = self.s
        sc, p = self.scan()
        target = f"{s.pfx}/etc/osmocom/osmo-bsc.cfg"
        time.sleep(1.1)
        with open(target, "a") as f:
            f.write("! edited after the scan\n")
        removed, skipped = self.apply(p)
        self.assertIn((target, "changed since the scan"), [(i.path, why) for i, why in skipped])
        self.assertTrue(os.path.exists(target))

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_permission_denied_is_reported(self):
        s = self.s
        sc, p = self.scan()
        os.chmod(f"{s.pfx}/bin", 0o555)
        try:
            removed, skipped = self.apply(p)
        finally:
            os.chmod(f"{s.pfx}/bin", 0o755)
        why = dict((i.path, r) for i, r in skipped)
        self.assertIn("needs root", why.get(f"{s.pfx}/bin/osmo-bsc", ""))
        self.assertTrue(os.path.exists(f"{s.pfx}/bin/osmo-bsc"))

    def test_protected_paths(self):
        s = self.s
        sc = s.scanner()
        self.assertTrue(sc.is_protected(s.backup))
        self.assertTrue(sc.is_protected(f"{s.backup}/gsm.bundle"))
        self.assertTrue(sc.is_protected(f"{s.home}/dev"))           # would remove the backup below it
        self.assertFalse(sc.is_protected(f"{s.home}/osmocom"))

    def test_elf_reader(self):
        s = self.s
        self.assertEqual(cleanup.elf_dynamic(f"{s.pfx}/lib/libosmocore.so.22.4.2")[0], "libosmocore.so.22")
        self.assertIn("libosmocore.so.22", cleanup.elf_dynamic(f"{s.pfx}/bin/osmo-bsc")[1])
        self.assertIsNone(cleanup.elf_dynamic(f"{s.pfx}/etc/other.conf"))
        self.assertFalse(cleanup.is_gsm_lib_name("libosmo-fl2k.so.0"))
        self.assertTrue(cleanup.is_gsm_lib_name("libosmo-gprs-llc.so.1"))

    def test_remote_parsing(self):
        P = cleanup.parse_remote
        self.assertEqual(P("https://gitea.osmocom.org/osmocom/libosmocore"), ("gitea.osmocom.org",
                                                                               "osmocom/libosmocore"))
        self.assertEqual(P("git@github.com:rouhi-dev/osmo-bsc.git"), ("github.com", "rouhi-dev/osmo-bsc.git"))
        self.assertEqual(P("ssh://git@gitea.osmocom.org:2222/x/osmo-bsc")[0], "gitea.osmocom.org")
        self.assertEqual(P("/srv/git/gsm.git"), ("", "/srv/git/gsm.git"))
        # look-alikes: the host decides, not a path or user part that mentions osmocom
        for url in ("https://evil.example/gitea.osmocom.org/osmocom/libosmocore",
                    "https://gitea.osmocom.org.evil.example/osmocom/libosmocore",
                    "https://osmocom.org@evil.example/osmocom/libosmocore"):
            s = self.s
            r = s.repo(f"{s.home}/lookalike/{abs(hash(url))}/libosmocore", url)
            sc, p = self.scan()
            self.assertTrue(r not in p or p[r].action != "remove", url)
            self.assertFalse(sc.repos[r]["verified"], url)

    # ------------------------------------------------------------------ command line
    def test_cli(self):
        s = self.s
        gsm_lab = os.path.join(os.path.dirname(os.path.dirname(helpers.HERE)), "gsm-lab")
        base = [sys.executable, gsm_lab, "cleanup", "--home", s.home, "--root", s.home, "--prefix", s.pfx,
                "--no-default-prefixes"]
        before = snapshot(s.root)
        r = subprocess.run(base, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Dry run: nothing changed", r.stdout)
        self.assertIn("== WILL REMOVE", r.stdout)
        r = subprocess.run(base + ["--apply"], input="no\n", capture_output=True, text=True)
        self.assertIn("Aborted: nothing changed", r.stdout)
        self.assertEqual(snapshot(s.root), before)


if __name__ == "__main__":
    unittest.main()
