"""Exercise the CI publisher against a real bare remote, injecting Git failures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


class PublishTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.remote = self.root / "remote.git"
        self.work = self.root / "runner"
        self.git("init", "--bare", "-b", "main", self.remote, cwd=self.root)
        self.git("clone", self.remote, self.work, cwd=self.root)
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.org")
        (self.work / "games").mkdir()
        for name in ["one", "two"]:
            (self.work / "games" / f"{name}.nix").write_text("old")
        self.git("add", "games")
        self.git("commit", "-m", "seed")
        self.git("push", "origin", "main")
        self.base = self.git("rev-parse", "HEAD")
        shutil.copytree(ROOT / ".github", self.work / ".github")
        # Remove waits only; keep every retry and use the production entry point.
        source = (ROOT / "update.nu").read_text()
        line = next(s for s in source.splitlines() if s.startswith("const RETRY_DELAYS"))
        (self.work / "update.nu").write_text(source.replace(line, "const RETRY_DELAYS = [0sec 0sec 0sec 0sec]"))
        self.env = dict(os.environ, GITHUB_SHA=self.base, UPDATE_RESULT="success",
                        MATRIX=json.dumps({"include": [{"catalog": n} for n in ["one", "two", "noop"]]}))
        for name in ["one", "two", "noop"]:
            ready = self.work / "incoming"
            ready.mkdir(exist_ok=True)
            (ready / f"{name}.json").write_text(json.dumps({"base": self.base, "changed": name != "noop"}))
            if name != "noop":
                (ready / f"{name}.nix").write_text("generated")

    def git(self, *args, cwd=None):
        return subprocess.check_output([GIT, *args], cwd=cwd or self.work,
                                       stderr=subprocess.PIPE, text=True).strip()

    def inject(self, script):
        directory = self.root / "bin"
        directory.mkdir()
        shim = directory / "git"
        shim.write_text("#!/usr/bin/env bash\nset -eu\n" + script + '\nexec "$REAL_GIT" "$@"\n')
        shim.chmod(0o755)
        self.env.update(REAL_GIT=GIT, MARK=str(self.root / "attempts"),
                        PATH=str(directory) + os.pathsep + self.env["PATH"])

    def publish(self, ok=True):
        result = subprocess.run(["nu", "--no-config-file", ".github/publish-ready.nu"],
                                cwd=self.work, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result.stderr

    def test_remote_errors_and_lost_acknowledgement(self):
        self.inject("""
if [[ $1 == fetch && ! -f $MARK.fetch ]]; then
  touch "$MARK.fetch"; echo 'HTTP: The requested URL returned error: 503' >&2; exit 1
fi
if [[ ${3-} == push ]]; then
  if [[ ! -f $MARK ]]; then
    touch "$MARK"; echo 'remote: fatal error in commit_refs' >&2; exit 1
  fi
  "$REAL_GIT" "$@"
  echo 'remote: Internal Server Error' >&2; exit 1
fi""")
        log = self.publish()
        self.assertIn("Fetch: remote service error", log)
        self.assertIn("Push: remote service error", log)
        self.assertEqual(self.git("rev-list", "--count", "main", cwd=self.remote), "2")
        for name in ["one", "two"]:
            self.assertEqual(self.git("show", f"main:games/{name}.nix", cwd=self.remote), "generated")

    def test_actual_competing_push_preserves_newer_catalog(self):
        other = self.root / "other"
        self.git("clone", self.remote, other)
        (other / "games/one.nix").write_text("concurrent")
        self.git("-c", "user.name=other", "-c", "user.email=o@example.org", "commit", "-am", "concurrent", cwd=other)
        self.env["OTHER"] = str(other)
        self.inject("""
if [[ ${3-} == push && ! -f $MARK ]]; then
  touch "$MARK"; "$REAL_GIT" -C "$OTHER" push origin main
fi""")
        self.assertIn("branch contention", self.publish())
        self.assertEqual(self.git("show", "main:games/one.nix", cwd=self.remote), "concurrent")
        self.assertEqual(self.git("show", "main:games/two.nix", cwd=self.remote), "generated")

    def test_protection_fails_immediately(self):
        self.inject("""
if [[ ${3-} == push ]]; then
  echo x >> "$MARK"; echo 'remote: GH013 repository rule violation' >&2; exit 1
fi""")
        self.assertIn("GH013", self.publish(ok=False))
        self.assertEqual((self.root / "attempts").read_text(), "x\n")

    def test_partial_matrix_and_missing_artifact(self):
        (self.work / "incoming/two.json").unlink()
        self.assertIn("Missing", self.publish(ok=False))
        self.env["UPDATE_RESULT"] = "failure"
        self.publish()
        self.assertEqual(self.git("show", "main:games/two.nix", cwd=self.remote), "old")
        self.assertEqual(self.git("show", "main:games/one.nix", cwd=self.remote), "generated")
