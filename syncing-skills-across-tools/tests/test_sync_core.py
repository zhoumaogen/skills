import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_core.py"
SPEC = importlib.util.spec_from_file_location("sync_core", MODULE_PATH)
assert SPEC and SPEC.loader
sync_core = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync_core
SPEC.loader.exec_module(sync_core)


class SyncCoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def _make_skill(self, tool, name, files=None):
        source = self.root / tool / "skills" / name
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Use when testing.\n---\n",
            encoding="utf-8",
        )
        for relative, content in (files or {}).items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return source

    def _tree_snapshot(self, root):
        if not root.exists():
            return ()
        return tuple(
            (p.relative_to(root).as_posix(), p.is_dir(), p.read_bytes() if p.is_file() else b"")
            for p in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        )

    # -- validate_source ----------------------------------------------------

    def test_validate_source_requires_regular_skill_md(self):
        source = self.root / ".codex" / "skills" / "demo"
        source.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "SKILL.md"):
            sync_core.validate_source(source)

    def test_validate_source_rejects_missing_directory(self):
        with self.assertRaises((ValueError, OSError)):
            sync_core.validate_source(self.root / "nope")

    # -- discovery ----------------------------------------------------------

    def test_discovery_only_uses_home_and_direct_child_skills(self):
        source = self._make_skill(".codex", "demo")
        peer = self.root / ".cursor" / "skills"
        peer.mkdir(parents=True)
        nested = self.root / "project" / "cache" / "skills"
        nested.mkdir(parents=True)

        roots = sync_core.discover_skills_roots(
            source, self.root, (), {"HOME": str(self.root)}
        )

        self.assertIn(peer.resolve(), roots)
        self.assertNotIn(nested.resolve(), roots)

    def test_discovery_reports_permission_denied_instead_of_returning_no_roots(self):
        source = self._make_skill(".codex", "demo")
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ValueError, "permission.*user home"):
                sync_core.discover_skills_roots(source, self.root, (), {})

    def test_explicit_tool_root_and_skills_root_are_deduplicated(self):
        source = self._make_skill(".codex", "demo")
        tool_root = self.root / ".cursor"
        skills_root = tool_root / "skills"
        skills_root.mkdir(parents=True)
        roots = sync_core.discover_skills_roots(
            source, self.root, (tool_root, skills_root), {"HOME": str(self.root)}
        )
        self.assertEqual(roots.count(skills_root.resolve()), 1)

    def test_link_like_skills_root_is_rejected(self):
        source = self._make_skill(".codex", "demo")
        real = self.root / "real-skills"
        real.mkdir()
        linked = self.root / ".cursor" / "skills"
        linked.parent.mkdir()
        try:
            linked.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"host cannot create directory links: {error}")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            sync_core.discover_skills_roots(source, self.root, (linked,), {})

    def test_userprofile_is_used_when_path_home_is_unusable(self):
        source = self._make_skill(".codex", "demo")
        peer = self.root / ".cursor" / "skills"
        peer.mkdir(parents=True)
        with mock.patch.object(
            sync_core.Path, "home", return_value=self.root / "missing-home"
        ):
            roots = sync_core.discover_skills_roots(
                source, None, (), {"USERPROFILE": str(self.root)}
            )
        self.assertIn(peer.resolve(), roots)

    def test_source_own_root_is_discovered_then_skipped_at_execute(self):
        # The source's own skills root should appear in discovery so it can
        # be reported and later skipped, not silently hidden.
        source = self._make_skill(".codex", "demo")
        roots = sync_core.discover_skills_roots(
            source, self.root, (), {"HOME": str(self.root)}
        )
        own_root = (self.root / ".codex" / "skills").resolve()
        self.assertIn(own_root, roots)

    # -- manifest & planning ------------------------------------------------

    def test_build_manifest_is_empty_for_missing_root(self):
        self.assertEqual(sync_core.build_manifest(self.root / "missing"), {})

    def test_plan_reports_create_replace_and_delete_without_writing(self):
        source = self._make_skill(".codex", "demo", {"a.txt": "new", "b.txt": "add"})
        skills_root = self.root / ".cursor" / "skills"
        target = skills_root / "demo"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Use when testing.\n---\n",
            encoding="utf-8",
        )
        (target / "a.txt").write_text("old", encoding="utf-8")
        (target / "stale.txt").write_text("remove", encoding="utf-8")

        before = self._tree_snapshot(skills_root)
        plan = sync_core.plan_target(source, skills_root)
        after = self._tree_snapshot(skills_root)

        self.assertEqual(before, after)
        self.assertEqual(
            {(op.action, op.relative_path) for op in plan.operations},
            {("replace", "a.txt"), ("create", "b.txt"), ("delete", "stale.txt")},
        )

    # -- fingerprint --------------------------------------------------------

    def test_fingerprint_is_stable_and_changes_with_source(self):
        source = self._make_skill(".codex", "demo", {"a.txt": "x"})
        peer = self.root / ".cursor" / "skills"
        peer.mkdir(parents=True)
        plan = sync_core.plan_target(source, peer)
        manifest = sync_core.build_manifest(source)
        fp1 = sync_core.plan_fingerprint(manifest, [plan])
        fp2 = sync_core.plan_fingerprint(manifest, [plan])
        self.assertEqual(fp1, fp2)

        (source / "a.txt").write_text("changed", encoding="utf-8")
        manifest2 = sync_core.build_manifest(source)
        fp3 = sync_core.plan_fingerprint(manifest2, [plan])
        self.assertNotEqual(fp1, fp3)

    def test_fingerprint_changes_when_target_added_or_removed(self):
        source = self._make_skill(".codex", "demo")
        peer_a = self.root / ".a" / "skills"
        peer_b = self.root / ".b" / "skills"
        peer_a.mkdir(parents=True)
        peer_b.mkdir(parents=True)
        plan_a = sync_core.plan_target(source, peer_a)
        plan_b = sync_core.plan_target(source, peer_b)
        manifest = sync_core.build_manifest(source)
        fp_both = sync_core.plan_fingerprint(manifest, [plan_a, plan_b])
        fp_one = sync_core.plan_fingerprint(manifest, [plan_a])
        self.assertNotEqual(fp_both, fp_one)

    # -- approval selection -------------------------------------------------

    def test_select_approved_plans_matches_destination_paths(self):
        source = self._make_skill(".codex", "demo")
        peer_a = self.root / ".a" / "skills"
        peer_b = self.root / ".b" / "skills"
        peer_a.mkdir(parents=True)
        peer_b.mkdir(parents=True)
        plan_a = sync_core.plan_target(source, peer_a)
        plan_b = sync_core.plan_target(source, peer_b)
        approved = sync_core.select_approved_plans(
            [plan_a, plan_b], [peer_a / "demo"]
        )
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0].destination, plan_a.destination)

    def test_select_approved_plans_empty_when_none_match(self):
        source = self._make_skill(".codex", "demo")
        peer = self.root / ".a" / "skills"
        peer.mkdir(parents=True)
        plan = sync_core.plan_target(source, peer)
        approved = sync_core.select_approved_plans([plan], [self.root / "nowhere" / "demo"])
        self.assertEqual(approved, ())

    # -- execution ----------------------------------------------------------

    def test_execute_plan_creates_missing_target_and_preserves_unicode(self):
        source = self._make_skill(".codex", "demo", {"data/desc.txt": "content"})
        root = self.root / ".cursor" / "skills"
        root.mkdir(parents=True)
        result = sync_core.execute_plan(source, sync_core.plan_target(source, root))
        self.assertEqual(result.status, "success")
        self.assertEqual((root / "demo" / "data" / "desc.txt").read_text(encoding="utf-8"), "content")

    def test_execute_plan_noop_leaves_tree_unchanged(self):
        source = self._make_skill(".codex", "demo", {"same.txt": "same"})
        root = self.root / ".cursor" / "skills"
        shutil.copytree(source, root / "demo")
        before = self._tree_snapshot(root)
        result = sync_core.execute_plan(source, sync_core.plan_target(source, root))
        self.assertEqual(result.status, "skipped")
        self.assertEqual(before, self._tree_snapshot(root))

    def test_execute_plan_skips_source_directory(self):
        source = self._make_skill(".codex", "demo")
        own_root = self.root / ".codex" / "skills"
        result = sync_core.execute_plan(source, sync_core.plan_target(source, own_root))
        self.assertEqual(result.status, "skipped")

    def test_execute_plan_complete_mirror_removes_stale_files(self):
        source = self._make_skill(".codex", "demo", {"keep.txt": "v2"})
        root = self.root / ".cursor" / "skills"
        target = root / "demo"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("v1", encoding="utf-8")
        (target / "stale.txt").write_text("bye", encoding="utf-8")
        result = sync_core.execute_plan(source, sync_core.plan_target(source, root))
        self.assertEqual(result.status, "success")
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "v2")
        self.assertFalse((target / "stale.txt").exists())

    def test_execute_plan_restores_backup_on_swap_failure(self):
        source = self._make_skill(".codex", "demo", {"a.txt": "new"})
        root = self.root / ".cursor" / "skills"
        target = root / "demo"
        target.mkdir(parents=True)
        (target / "old.txt").write_text("old", encoding="utf-8")

        calls: list = []
        real_replace = sync_core._atomic_replace

        def flaky_replace(src, dst):
            calls.append((str(src), str(dst)))
            # First call: move destination -> backup (ok).
            # Second call: move staging -> destination (fail).
            if len(calls) == 2:
                raise OSError("injected swap failure")
            return real_replace(src, dst)

        with mock.patch.object(sync_core, "_atomic_replace", side_effect=flaky_replace):
            result = sync_core.execute_plan(source, sync_core.plan_target(source, root))

        self.assertEqual(result.status, "failed")
        # Original target content must be restored.
        self.assertTrue((target / "old.txt").exists())
        self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "old")
        # New content must not have leaked.
        self.assertFalse((target / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
