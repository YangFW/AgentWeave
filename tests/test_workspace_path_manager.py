from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.workspace_path_manager import (
    WorkspacePathError,
    WorkspacePathManager,
    _sanitize_path_segment,
)


class WorkspacePathManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.mgr = WorkspacePathManager(base_dir=self.base_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sanitize_path_segment(self) -> None:
        self.assertEqual(_sanitize_path_segment("valid-org", "org"), "valid-org")
        self.assertEqual(_sanitize_path_segment("user.123_abc", "user"), "user.123_abc")

        with self.assertRaises(WorkspacePathError):
            _sanitize_path_segment("../escaped", "org")

        with self.assertRaises(WorkspacePathError):
            _sanitize_path_segment("foo/bar", "org")

        with self.assertRaises(WorkspacePathError):
            _sanitize_path_segment("", "org")

    def test_get_paths_and_layout(self) -> None:
        paths = self.mgr.get_paths("my-org", "user-1", "project-alpha")
        expected_root = self.base_dir / "my-org" / "user-1" / "project-alpha"
        self.assertEqual(paths.root, expected_root)
        self.assertEqual(paths.code_dir, expected_root / "code")
        self.assertEqual(paths.state_dir, expected_root / "state")
        self.assertEqual(paths.artifacts_dir, expected_root / "artifacts")
        self.assertEqual(paths.logs_dir, expected_root / "logs")
        self.assertEqual(paths.codex_state_dir, expected_root / "state" / ".codex")
        self.assertEqual(paths.claude_state_dir, expected_root / "state" / ".claude")
        self.assertEqual(paths.claude_config_file, expected_root / "state" / ".claude.json")

    def test_ensure_workspace(self) -> None:
        paths = self.mgr.get_paths("org-a", "user-b", "ws-c")
        self.assertFalse(paths.root.exists())

        self.mgr.ensure_workspace(paths)

        self.assertTrue(paths.root.exists())
        self.assertTrue(paths.code_dir.is_dir())
        self.assertTrue(paths.state_dir.is_dir())
        self.assertTrue(paths.artifacts_dir.is_dir())
        self.assertTrue(paths.logs_dir.is_dir())
        self.assertTrue(paths.codex_state_dir.is_dir())
        self.assertTrue(paths.claude_state_dir.is_dir())
        self.assertTrue(paths.claude_config_file.is_file())

    def test_get_docker_mounts(self) -> None:
        paths = self.mgr.get_paths("org-a", "user-b", "ws-c")
        mounts = self.mgr.get_docker_mounts(paths, container_workspace="/workspace", container_home="/home/node")

        self.assertIn(f"{paths.code_dir}:/workspace:rw", mounts)
        self.assertIn(f"{paths.artifacts_dir}:/workspace/artifacts:rw", mounts)
        self.assertIn(f"{paths.codex_state_dir}:/home/node/.codex:rw", mounts)
        self.assertIn(f"{paths.claude_state_dir}:/home/node/.claude:rw", mounts)
        self.assertIn(f"{paths.claude_config_file}:/home/node/.claude.json:rw", mounts)

    def test_collect_new_artifacts(self) -> None:
        paths = self.mgr.get_paths("org-a", "user-b", "ws-c")
        self.mgr.ensure_workspace(paths)

        art_file = paths.artifacts_dir / "report.pdf"
        art_file.write_text("dummy pdf", encoding="utf-8")

        code_file = paths.code_dir / "main.py"
        code_file.write_text("print(1)", encoding="utf-8")

        hidden_file = paths.code_dir / ".git" / "config"
        hidden_file.parent.mkdir(exist_ok=True)
        hidden_file.write_text("git config", encoding="utf-8")

        collected = self.mgr.collect_new_artifacts(paths)
        collected_names = {f.name for f in collected}

        self.assertIn("report.pdf", collected_names)
        self.assertIn("main.py", collected_names)
        self.assertNotIn("config", collected_names)


if __name__ == "__main__":
    unittest.main()
