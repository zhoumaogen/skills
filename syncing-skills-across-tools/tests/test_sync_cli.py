import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_skills.py"
SCRIPT_DIR = SCRIPT.parent
sys.path.insert(0, str(SCRIPT_DIR))
import sync_core
import sync_skills as sync_cli


class SyncCliTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.home = Path(self._temporary.name)
        self.source = self.home / ".codex" / "skills" / "demo"
        self.source.mkdir(parents=True)
        (self.source / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Use when testing.\n---\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self._temporary.cleanup()

    def _run(self, *arguments):
        clean_env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=clean_env,
        )

    def _preview_json(self):
        result = self._run(
            "--source", str(self.source),
            "--user-home", str(self.home),
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    # -- argument validation ------------------------------------------------

    def test_apply_requires_approved_target_and_matching_fingerprint(self):
        result = self._run("--source", str(self.source), "--apply")
        self.assertEqual(result.returncode, 2)
        self.assertIn("approved-target", result.stderr)

    def test_apply_requires_plan_fingerprint(self):
        result = self._run(
            "--source", str(self.source), "--apply",
            "--approved-target", str(self.home / ".x" / "skills" / "demo"),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("plan-fingerprint", result.stderr)

    def test_dry_run_and_apply_are_mutually_exclusive(self):
        result = self._run(
            "--source", str(self.source), "--dry-run", "--apply",
        )
        self.assertNotEqual(result.returncode, 0)

    # -- preview / dry-run --------------------------------------------------

    def test_no_peer_target_requests_user_input_without_writing(self):
        result = self._run(
            "--source", str(self.source),
            "--user-home", str(self.home),
            "--json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 3)
        self.assertTrue(payload["requires_user_input"])

    def test_preview_shows_targets_and_fingerprint(self):
        peer = self.home / ".cursor" / "skills"
        peer.mkdir(parents=True)
        payload = self._preview_json()
        self.assertEqual(payload["mode"], "dry-run")
        self.assertTrue(payload["plan_fingerprint"])
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(len(payload["targets"]), 1)
        self.assertEqual(payload["targets"][0]["creates"], 1)

    # -- apply behaviour ----------------------------------------------------

    def test_apply_writes_only_the_approved_target(self):
        approved_root = self.home / ".cursor" / "skills"
        unapproved_root = self.home / ".claude" / "skills"
        approved_root.mkdir(parents=True)
        unapproved_root.mkdir(parents=True)
        preview = self._run(
            "--source", str(self.source),
            "--user-home", str(self.home),
            "--json",
        )
        payload = json.loads(preview.stdout)
        approved_destination = approved_root / "demo"
        result = self._run(
            "--source", str(self.source),
            "--user-home", str(self.home),
            "--apply",
            "--plan-fingerprint", payload["plan_fingerprint"],
            "--approved-target", str(approved_destination),
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((approved_destination / "SKILL.md").is_file())
        self.assertFalse((unapproved_root / "demo").exists())

    def test_changed_plan_is_rejected_without_writing(self):
        peer = self.home / ".cursor" / "skills"
        peer.mkdir(parents=True)
        preview = self._run(
            "--source", str(self.source),
            "--user-home", str(self.home),
            "--json",
        )
        payload = json.loads(preview.stdout)
        (self.source / "changed.txt").write_text("changed", encoding="utf-8")
        result = self._run(
            "--source", str(self.source),
            "--user-home", str(self.home),
            "--apply",
            "--plan-fingerprint", payload["plan_fingerprint"],
            "--approved-target", str(peer / "demo"),
            "--json",
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse((peer / "demo").exists())

    # -- pure function ------------------------------------------------------

    def test_exit_code_for_results_reports_partial_failure(self):
        success = sync_core.TargetResult(Path("ok"), "success", "")
        failure = sync_core.TargetResult(Path("bad"), "failed", "denied")
        self.assertEqual(
            sync_cli.exit_code_for_results((success,)), sync_cli.ExitCode.SUCCESS
        )
        self.assertEqual(
            sync_cli.exit_code_for_results((success, failure)),
            sync_cli.ExitCode.PARTIAL_FAILURE,
        )


if __name__ == "__main__":
    unittest.main()
