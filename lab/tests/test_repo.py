"""Phase 0: `gsm-lab update` must never move a submodule that holds local work.

Builds a throwaway superproject with two submodules (local file:// repos), advances both upstream, then
makes one dirty and moves the other to an unrecorded commit before running update.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import helpers  # noqa: F401  (sys.path)
from gsmlab import repo

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow",
       "GIT_CONFIG_VALUE_0": "always"}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


class UpdateSafety(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="gsm-lab-repo-")
        os.environ.update({k: ENV[k] for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")})
        repo.ENV.update({k: ENV[k] for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")})
        for name in ("liba", "libb"):
            d = os.path.join(self.t, "up", name)
            os.makedirs(d)
            sh(d, "init", "-q", "-b", "main")
            open(os.path.join(d, "f"), "w").write("1\n")
            sh(d, "add", "f")
            sh(d, "commit", "-qm", "v1")
        sup = os.path.join(self.t, "up", "super")
        os.makedirs(sup)
        sh(sup, "init", "-q", "-b", "main")
        for name in ("liba", "libb"):
            sh(sup, "submodule", "add", "-q", os.path.join(self.t, "up", name), name)
        sh(sup, "commit", "-qm", "super v1")
        self.work = os.path.join(self.t, "work")
        sh(self.t, "clone", "-q", "--recurse-submodules", sup, self.work)
        # upstream moves both submodules to v2 and records that in the superproject
        for name in ("liba", "libb"):
            d = os.path.join(self.t, "up", name)
            open(os.path.join(d, "f"), "w").write("2\n")
            sh(d, "commit", "-qam", "v2")
            sh(os.path.join(sup, name), "pull", "-q", "origin", "main")
        sh(sup, "commit", "-qam", "super v2")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def test_update_moves_clean_and_keeps_local_work(self):
        a, b = os.path.join(self.work, "liba"), os.path.join(self.work, "libb")
        open(os.path.join(a, "f"), "w").write("my uncommitted work\n")          # liba: dirty
        open(os.path.join(b, "g"), "w").write("x\n")                             # libb: local commit
        sh(b, "add", "g")
        sh(b, "commit", "-qm", "local experiment")
        mine = sh(b, "rev-parse", "HEAD")
        out = []
        repo.update(out=out.append, root=self.work, show_status=False)
        text = "\n".join(out)
        self.assertIn("[SKIP] liba: has uncommitted changes", text)
        self.assertIn("[SKIP] libb: checked out at", text)
        with open(os.path.join(a, "f")) as f:
            self.assertEqual(f.read(), "my uncommitted work\n")
        self.assertEqual(sh(b, "rev-parse", "HEAD"), mine)

    def test_update_moves_clean_submodules(self):
        out = []
        self.assertEqual(repo.update(out=out.append, root=self.work, show_status=False), 0)
        self.assertIn("liba:", "\n".join(out))
        with open(os.path.join(self.work, "liba", "f")) as f:
            self.assertEqual(f.read(), "2\n")


if __name__ == "__main__":
    unittest.main()
