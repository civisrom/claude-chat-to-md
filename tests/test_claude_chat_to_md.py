from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import claude_chat_to_md as chat


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class ClaudeChatToMdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_projects_dir = chat.PROJECTS_DIR
        chat.PROJECTS_DIR = self.root / "projects"

    def tearDown(self) -> None:
        chat.PROJECTS_DIR = self.old_projects_dir
        self.tmp.cleanup()

    def test_discover_sessions_uses_summary_timestamp_and_cwd(self) -> None:
        sid = "a1b2c3d4"
        write_jsonl(
            chat.PROJECTS_DIR / "-home-claude-myapp" / f"{sid}.jsonl",
            [
                {
                    "type": "summary",
                    "summary": "Session title",
                    "timestamp": "2026-04-25T10:00:00Z",
                    "cwd": "/home/claude/myapp",
                }
            ],
        )

        sessions = chat.discover_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, sid)
        self.assertEqual(sessions[0].title, "Session title")
        self.assertEqual(sessions[0].display_project, "/home/claude/myapp")

    def test_project_filter_can_match_display_project(self) -> None:
        sessions = [
            chat.SessionInfo(
                path=Path("one.jsonl"),
                session_id="one",
                project_path="-encoded-path",
                cwd="/srv/real-project",
            ),
            chat.SessionInfo(
                path=Path("two.jsonl"),
                session_id="two",
                project_path="-other",
                cwd="/srv/other",
            ),
        ]

        filtered = [
            s
            for s in sessions
            if "real-project" in s.display_project.lower()
            or "real-project" in s.project_path.lower()
        ]

        self.assertEqual([s.session_id for s in filtered], ["one"])

    def test_no_tool_results_excludes_collapsed_tool_result(self) -> None:
        path = self.root / "session.jsonl"
        write_jsonl(
            path,
            [
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "content": "tool output",
                            }
                        ]
                    },
                }
            ],
        )
        session = chat.SessionInfo(path=path, session_id="sid", project_path="proj")

        markdown = chat.convert_session(session, include_tool_results=False)

        self.assertNotIn("tool output", markdown)
        self.assertNotIn("<details><summary>Tool Result</summary>", markdown)

    def test_subagent_attaches_by_tool_use_id(self) -> None:
        sid = "session-id"
        project_dir = self.root / "projects" / "-home-claude-myapp"
        session_path = project_dir / f"{sid}.jsonl"
        subagent_dir = project_dir / sid / "subagents"
        write_jsonl(
            session_path,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Task",
                                "input": {
                                    "description": "Audit",
                                    "subagent_type": "explorer",
                                    "prompt": "check it",
                                },
                            }
                        ]
                    },
                }
            ],
        )
        write_jsonl(
            subagent_dir / "agent-1.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "subagent result"}]},
                }
            ],
        )
        (subagent_dir / "agent-1.meta.json").write_text(
            json.dumps(
                {
                    "toolUseID": "toolu_1",
                    "agentType": "explorer",
                    "description": "Audit",
                }
            ),
            encoding="utf-8",
        )
        session = chat.SessionInfo(
            path=session_path,
            session_id=sid,
            project_path="-home-claude-myapp",
            subagent_dir=subagent_dir,
        )

        markdown = chat.convert_session(session, include_subagents=True)

        self.assertIn("<details><summary>Subagent Conversation</summary>", markdown)
        self.assertIn("subagent result", markdown)


if __name__ == "__main__":
    unittest.main()
