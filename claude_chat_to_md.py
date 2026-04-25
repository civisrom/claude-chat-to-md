#!/usr/bin/env python3
"""Convert Claude Code chat sessions (.jsonl) to Markdown.

Reads session JSONL files from ~/.claude/projects/ and produces clean,
readable Markdown — including subagent conversations.

Linux-only tool. Output files are UTF-8 and use filenames that are safe
to open on both Linux and Windows.

Usage:
    # List all sessions
    claude-chat-to-md --list

    # Convert a specific session (by UUID or partial match)
    claude-chat-to-md 2354ca15

    # Convert the most recent session for a project
    claude-chat-to-md --latest --project myapp

    # Convert all sessions
    claude-chat-to-md --all

    # Output to a specific file
    claude-chat-to-md 2354ca15 -o chat.md
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"


# --- helpers ----------------------------------------------------------------

# Parent tags that Claude Code / slash commands / hooks wrap around content
# we don't want in the Markdown output.
_SYSTEM_TAGS = (
    "ide_opened_file",
    "ide_selection",
    "system-reminder",
    "command-name",
    "command-message",
    "command-args",
    "local-command-stdout",
    "local-command-stderr",
    "user-prompt-submit-hook",
    "user-memory-input",
)
_SYSTEM_TAG_RE = re.compile(
    "|".join(rf"<{t}>.*?</{t}>" for t in _SYSTEM_TAGS),
    re.DOTALL,
)


def _strip_system_tags(text: str) -> str:
    """Remove paired system/IDE/hook tags from a text block."""
    return _SYSTEM_TAG_RE.sub("", text)


def _code_block(content: str, lang: str = "") -> str:
    """Wrap content in a fenced code block using a fence long enough to
    avoid collisions with backticks inside the content."""
    longest = 0
    for m in re.finditer(r"`+", content):
        longest = max(longest, len(m.group()))
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{content}\n{fence}"


def _format_unified_diff(old: str, new: str, file_path: str) -> str:
    """Return a unified-diff Markdown block for an Edit."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    # Ensure trailing newline so diff rows don't glue together.
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    rel = file_path.lstrip("/") or "file"
    diff = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )
    if not diff:
        diff = "(no textual difference)"
    return _code_block(diff.rstrip("\n"), "diff")


# Filesystem-safe names for outputs. Windows is the strict case: it forbids
# <>:"/\|?* and control chars, reserves CON/PRN/AUX/NUL/COM1-9/LPT1-9, and
# disallows trailing dots or spaces. Linux only forbids '/' and NUL, so a
# Windows-safe name is automatically Linux-safe.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str, max_len: int = 80) -> str:
    """Return a filename safe on both Linux and Windows filesystems."""
    s = _INVALID_FS_CHARS.sub("_", name)
    s = re.sub(r"\s+", "-", s)          # collapse whitespace to '-'
    s = re.sub(r"-+", "-", s)           # collapse repeated '-'
    s = s.strip("-. ")                  # no leading/trailing dot/dash/space
    if not s:
        s = "untitled"
    # Reserved device names are checked on the stem.
    stem = s.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        s = "_" + s
    return s[:max_len] or "untitled"


# --- session discovery ------------------------------------------------------


@dataclass
class SessionInfo:
    """Metadata about a discovered session."""

    path: Path
    session_id: str
    project_path: str
    title: str | None = None
    timestamp: str | None = None
    subagent_dir: Path | None = None
    cwd: str | None = None

    @property
    def display_project(self) -> str:
        """Human-readable project path.

        Claude Code stores the working directory in each JSONL record as
        a ``cwd`` field. If we captured it during discovery we use it
        verbatim — that's the authoritative original path. Otherwise we
        fall back to decoding the directory name under ~/.claude/projects/,
        which replaces every '/' in the absolute path with '-'. That
        encoding is lossy for directory names containing hyphens, so the
        fallback is best-effort.
        """
        if self.cwd:
            return self.cwd

        raw = self.project_path
        if not raw:
            return raw

        # Strip the leading dash (representing '/') and naively convert.
        # Directory names containing '-' will be split, but without the
        # original path we can't disambiguate — document it and move on.
        without_leading = raw[1:] if raw.startswith("-") else raw
        return "/" + without_leading.replace("-", "/")


def _read_session_metadata(
    jsonl_file: Path,
) -> tuple[str | None, str | None, str | None]:
    """Scan a JSONL file for (title, timestamp, cwd).

    Reads until all three fields are populated, then stops. Claude Code
    writes titles as either ``type: "ai-title"`` (with ``aiTitle``) or
    ``type: "summary"`` (with ``summary``). ``cwd`` appears on most
    user/assistant records; ``timestamp`` on any message.
    """
    title: str | None = None
    summary: str | None = None
    timestamp: str | None = None
    cwd: str | None = None
    try:
        with open(jsonl_file, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                obj_type = obj.get("type")
                if title is None and obj_type == "ai-title":
                    title = obj.get("aiTitle") or obj.get("title")
                elif summary is None and obj_type == "summary":
                    summary = obj.get("summary")
                if timestamp is None and obj.get("timestamp"):
                    timestamp = obj["timestamp"]
                if cwd is None and obj.get("cwd"):
                    cwd = obj["cwd"]
                if (title or summary) and timestamp and cwd:
                    break
    except OSError:
        pass
    return (title or summary), timestamp, cwd


def discover_sessions() -> list[SessionInfo]:
    """Find all session JSONL files under ~/.claude/projects/."""
    sessions: list[SessionInfo] = []
    if not PROJECTS_DIR.exists():
        return sessions

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            sid = jsonl_file.stem
            if sid.startswith("agent-"):
                continue

            title, timestamp, cwd = _read_session_metadata(jsonl_file)

            info = SessionInfo(
                path=jsonl_file,
                session_id=sid,
                project_path=project_dir.name,
                title=title,
                timestamp=timestamp,
                cwd=cwd,
            )

            subagent_dir = project_dir / sid / "subagents"
            if subagent_dir.is_dir():
                info.subagent_dir = subagent_dir

            sessions.append(info)

    sessions.sort(key=lambda s: s.timestamp or "", reverse=True)
    return sessions


def parse_messages(jsonl_path: Path) -> list[dict]:
    """Parse a JSONL file into an ordered list of message records."""
    messages = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") in ("user", "assistant"):
                messages.append(obj)
    return messages


# --- formatting -------------------------------------------------------------


def format_tool_use(content_block: dict) -> str:
    """Format a tool_use content block as Markdown."""
    name = content_block.get("name", "Unknown")
    inp = content_block.get("input", {}) or {}

    lines = [f"**Tool: {name}**"]

    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        if desc:
            lines.append(f"*{desc}*")
        lines.append(_code_block(cmd, "bash"))

    elif name == "Read":
        fp = inp.get("file_path", "")
        offset = inp.get("offset")
        limit = inp.get("limit")
        range_info = ""
        if offset is not None or limit is not None:
            range_info = f" (lines {offset or 0}..{(offset or 0) + (limit or 0)})"
        lines.append(f"Reading `{fp}`{range_info}")

    elif name == "Write":
        fp = inp.get("file_path", "")
        content = inp.get("content", "")
        lines.append(f"Writing `{fp}`")
        if content:
            content_lines = content.split("\n")
            if len(content_lines) > 30:
                preview = "\n".join(content_lines[:15])
                preview += f"\n\n... ({len(content_lines) - 30} lines omitted) ...\n\n"
                preview += "\n".join(content_lines[-15:])
            else:
                preview = content
            ext = Path(fp).suffix.lstrip(".")
            # Only use extension as lang hint if it looks like a common one.
            lang = ext if ext.isalnum() and len(ext) <= 10 else ""
            lines.append(_code_block(preview, lang))

    elif name == "Edit":
        fp = inp.get("file_path", "")
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        replace_all = inp.get("replace_all", False)
        suffix = " (replace_all)" if replace_all else ""
        lines.append(f"Editing `{fp}`{suffix}")
        if old or new:
            lines.append(_format_unified_diff(old, new, fp))

    elif name == "MultiEdit":
        fp = inp.get("file_path", "")
        edits = inp.get("edits", []) or []
        lines.append(f"Editing `{fp}` ({len(edits)} change{'s' if len(edits) != 1 else ''})")
        for i, edit in enumerate(edits, 1):
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            lines.append(f"\n*Change {i}:*")
            lines.append(_format_unified_diff(old, new, fp))

    elif name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", ".")
        lines.append(f"Searching for `{pattern}` in `{path}`")

    elif name == "Glob":
        pattern = inp.get("pattern", "")
        lines.append(f"Finding files matching `{pattern}`")

    elif name in ("Agent", "Task"):
        desc = inp.get("description", "")
        subtype = inp.get("subagent_type", "general-purpose")
        lines.append(f"Spawning **{subtype}** agent: *{desc}*")
        prompt = inp.get("prompt", "")
        if prompt:
            if len(prompt) > 500:
                prompt = prompt[:500] + "..."
            lines.append(f"\n> {prompt}")

    elif name == "WebSearch":
        query = inp.get("query", "")
        lines.append(f"Searching the web: `{query}`")

    elif name == "WebFetch":
        url = inp.get("url", "")
        prompt = inp.get("prompt", "")
        lines.append(f"Fetching `{url}`")
        if prompt:
            prompt_short = prompt if len(prompt) <= 500 else prompt[:500] + "..."
            lines.append(f"\n> {prompt_short}")

    elif name == "TodoWrite":
        todos = inp.get("todos", []) or []
        mark = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        for t in todos:
            status = t.get("status", "pending")
            text = t.get("content", "")
            lines.append(f"- {mark.get(status, '[ ]')} {text}")

    elif name == "NotebookEdit":
        fp = inp.get("notebook_path", "")
        cell_id = inp.get("cell_id", "")
        edit_mode = inp.get("edit_mode", "replace")
        suffix = f" (id: {cell_id})" if cell_id else ""
        lines.append(f"Notebook {edit_mode} in `{fp}`{suffix}")
        src = inp.get("new_source", "")
        if src:
            lines.append(_code_block(src))

    else:
        # Generic: show input as JSON
        if inp:
            blob = json.dumps(inp, indent=2, ensure_ascii=False)
            if len(blob) > 500:
                blob = blob[:500] + "\n..."
            lines.append(_code_block(blob, "json"))

    return "\n".join(lines)


def format_tool_result(content_block: dict) -> str:
    """Format a tool_result content block as Markdown."""
    content = content_block.get("content", "")
    is_error = content_block.get("is_error", False)

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, dict) and item.get("type") == "image":
                parts.append("*[image]*")
            else:
                parts.append(str(item))
        content = "\n".join(parts)

    if not content:
        return ""

    prefix = "**Error:**\n" if is_error else ""
    lines = content.split("\n")
    if len(lines) > 50:
        content = "\n".join(lines[:25])
        content += f"\n\n... ({len(lines) - 50} lines omitted) ...\n\n"
        content += "\n".join(lines[-25:])

    return f"{prefix}{_code_block(content)}"


def format_content(content: list | str) -> str:
    """Format a message's content array into Markdown."""
    if isinstance(content, str):
        return _strip_system_tags(content).strip()

    parts = []
    for block in content:
        if isinstance(block, str):
            stripped = _strip_system_tags(block).strip()
            if stripped:
                parts.append(stripped)
        elif not isinstance(block, dict):
            continue
        elif block.get("type") == "text":
            text = _strip_system_tags(block.get("text", "")).strip()
            if text:
                parts.append(text)
        elif block.get("type") == "tool_use":
            parts.append(format_tool_use(block))
        elif block.get("type") == "tool_result":
            parts.append(format_tool_result(block))

    return "\n\n".join(parts)


# --- subagent handling ------------------------------------------------------


def _load_subagent_meta(meta_path: Path) -> dict:
    """Read and parse a subagent's .meta.json file."""
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _meta_tool_use_id(meta: dict) -> str | None:
    """Best-effort extraction of the parent Agent tool_use id from meta."""
    for key in ("toolUseID", "tool_use_id", "toolUseId", "parentToolUseId", "parent_tool_use_id"):
        if meta.get(key):
            return str(meta[key])
    return None


def convert_subagent(jsonl_path: Path, meta: dict) -> str:
    """Convert a subagent session into a Markdown section."""
    agent_type = meta.get("agentType", meta.get("subagent_type", "unknown"))
    description = meta.get("description", "Subagent")

    messages = parse_messages(jsonl_path)
    lines = [f"#### Subagent: {description}", f"*Type: {agent_type}*\n"]

    for msg in messages:
        role = msg.get("type", msg.get("message", {}).get("role", "unknown"))
        content = msg.get("message", {}).get("content", [])
        formatted = format_content(content)
        if not formatted:
            continue
        if role == "user":
            lines.append(f"**Prompt:**\n\n{formatted}\n")
        elif role == "assistant":
            lines.append(f"{formatted}\n")

    return "\n".join(lines)


# --- session conversion -----------------------------------------------------


def convert_session(
    session: SessionInfo,
    include_subagents: bool = True,
    include_tool_results: bool = True,
) -> str:
    """Convert a full session to Markdown."""
    messages = parse_messages(session.path)

    # Collect subagents: both an id->markdown map AND a tool_use_id->id map.
    subagent_md: dict[str, str] = {}          # agent_id -> markdown
    tid_to_agent: dict[str, str] = {}         # parent tool_use_id -> agent_id
    agent_desc_map: dict[str, list[str]] = {}  # description -> [agent_ids] (fallback)

    if include_subagents and session.subagent_dir:
        for meta_file in session.subagent_dir.glob("*.meta.json"):
            agent_id = meta_file.name.removesuffix(".meta.json")
            jsonl_file = session.subagent_dir / f"{agent_id}.jsonl"
            if not jsonl_file.exists():
                continue
            meta = _load_subagent_meta(meta_file)
            subagent_md[agent_id] = convert_subagent(jsonl_file, meta)

            tid = _meta_tool_use_id(meta)
            if tid:
                tid_to_agent[tid] = agent_id
            desc = (meta.get("description") or "").strip()
            if desc:
                agent_desc_map.setdefault(desc, []).append(agent_id)

    # Track which agent_ids have already been attached, so description-based
    # fallback matching doesn't reuse the same subagent twice.
    attached: set[str] = set()

    def _resolve_agent(block: dict) -> str | None:
        """Return an agent_id for an Agent tool_use block, preferring the
        authoritative tool_use_id mapping and falling back to description."""
        tid = block.get("id", "")
        if tid and tid in tid_to_agent:
            aid = tid_to_agent[tid]
            if aid in subagent_md:
                return aid
        desc = (block.get("input", {}) or {}).get("description", "").strip()
        for aid in agent_desc_map.get(desc, []):
            if aid not in attached:
                return aid
        return None

    lines: list[str] = []

    # Header
    title = session.title or "Untitled Session"
    ts = session.timestamp or ""
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts = dt.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            pass

    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**Session:** `{session.session_id}`  ")
    lines.append(f"**Project:** `{session.display_project}`  ")
    if ts:
        lines.append(f"**Date:** {ts}  ")
    lines.append("")
    lines.append("---")
    lines.append("")

    for msg in messages:
        role = msg.get("type", "")
        content = msg.get("message", {}).get("content", [])

        if role == "user":
            # tool_result messages are rendered as a collapsed section
            if isinstance(content, list) and content and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                if include_tool_results:
                    formatted = format_content(content)
                    if formatted.strip():
                        lines.append(
                            f"<details><summary>Tool Result</summary>\n\n{formatted}\n\n</details>\n"
                        )
                continue

            formatted = format_content(content)
            if formatted.strip():
                lines.append(f"## User\n\n{formatted}\n")

        elif role == "assistant":
            formatted_parts: list[str] = []
            agent_blocks: list[dict] = []

            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        if block.get("name") in ("Agent", "Task"):
                            agent_blocks.append(block)
                        formatted_parts.append(format_tool_use(block))
                    elif btype == "text":
                        text = _strip_system_tags(block.get("text", "")).strip()
                        if text:
                            formatted_parts.append(text)
                    elif btype == "tool_result":
                        if include_tool_results:
                            r = format_tool_result(block)
                            if r:
                                formatted_parts.append(r)

            formatted = "\n\n".join(formatted_parts)
            if formatted.strip():
                lines.append(f"## Assistant\n\n{formatted}\n")

            # Attach any subagent conversations spawned from this message.
            for block in agent_blocks:
                aid = _resolve_agent(block)
                if aid and aid not in attached:
                    attached.add(aid)
                    lines.append("<details><summary>Subagent Conversation</summary>\n")
                    lines.append(subagent_md[aid])
                    lines.append("</details>\n")

    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------


def list_sessions(sessions: list[SessionInfo], out: TextIO = sys.stdout) -> None:
    """Print a table of discovered sessions."""
    if not sessions:
        print("No sessions found.", file=out)
        return

    print(f"{'#':<4} {'Date':<20} {'ID':<12} {'Title':<50} {'Project'}", file=out)
    print("-" * 120, file=out)
    for i, s in enumerate(sessions, 1):
        ts = ""
        if s.timestamp:
            try:
                dt = datetime.fromisoformat(s.timestamp.replace("Z", "+00:00"))
                ts = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                ts = s.timestamp[:16]
        title = (s.title or "Untitled")[:50]
        sid = s.session_id[:10] + ".."
        proj = s.display_project
        parts = proj.split("/")
        if len(parts) > 3:
            proj = ".../" + "/".join(parts[-3:])
        print(f"{i:<4} {ts:<20} {sid:<12} {title:<50} {proj}", file=out)


def find_session(sessions: list[SessionInfo], query: str) -> SessionInfo | None:
    """Find a session by UUID prefix, index number, or title substring.

    UUID prefix is checked first so that a short hex id is not accidentally
    swallowed by the integer index lookup.
    """
    # 1. UUID prefix (at least 2 chars and matches hex shape)
    if len(query) >= 2:
        matches = [s for s in sessions if s.session_id.startswith(query)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Ambiguous — take none; caller must disambiguate.
            return None

    # 2. Index from --list
    try:
        idx = int(query) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]
    except ValueError:
        pass

    # 3. Title substring
    q = query.lower()
    for s in sessions:
        if s.title and q in s.title.lower():
            return s

    return None


def _build_output_filename(session: SessionInfo) -> str:
    """Build a collision-resistant, cross-platform output filename."""
    date_part = ""
    if session.timestamp:
        try:
            dt = datetime.fromisoformat(session.timestamp.replace("Z", "+00:00"))
            date_part = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    title_part = _sanitize_filename(session.title or "untitled", max_len=60)
    sid_part = session.session_id[:8]
    prefix = f"{date_part}-" if date_part else ""
    return f"{prefix}{title_part}-{sid_part}.md"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Claude Code chat sessions to Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Session UUID (prefix), index number from --list, or title substring",
    )
    parser.add_argument("--list", "-l", action="store_true", help="List all sessions")
    parser.add_argument("--latest", action="store_true", help="Convert the most recent session")
    parser.add_argument("--all", action="store_true", help="Convert all sessions")
    parser.add_argument("--project", "-p", help="Filter by project path substring")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument(
        "--no-subagents", action="store_true", help="Exclude subagent conversations"
    )
    parser.add_argument(
        "--no-tool-results", action="store_true", help="Exclude tool results"
    )
    parser.add_argument(
        "--output-dir", "-d", help="Output directory (for --all mode)"
    )

    args = parser.parse_args()
    sessions = discover_sessions()

    if args.project:
        needle = args.project.lower()
        sessions = [
            s
            for s in sessions
            if needle in s.display_project.lower() or needle in s.project_path.lower()
        ]

    if args.list:
        list_sessions(sessions)
        return

    if not args.session and not args.latest and not args.all:
        parser.print_help()
        print("\nUse --list to see available sessions.", file=sys.stderr)
        sys.exit(1)

    include_subagents = not args.no_subagents
    include_tool_results = not args.no_tool_results

    if args.all:
        out_dir = Path(args.output_dir) if args.output_dir else Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)
        for s in sessions:
            md = convert_session(s, include_subagents, include_tool_results)
            filename = _build_output_filename(s)
            out_path = out_dir / filename
            out_path.write_text(md, encoding="utf-8")
            print(f"Wrote {out_path}", file=sys.stderr)
        return

    if args.latest:
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            sys.exit(1)
        session = sessions[0]
    else:
        session = find_session(sessions, args.session)
        if not session:
            print(f"Session not found or ambiguous: {args.session}", file=sys.stderr)
            print("Use --list to see available sessions.", file=sys.stderr)
            sys.exit(1)

    md = convert_session(session, include_subagents, include_tool_results)

    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        # Reconfigure stdout to UTF-8 so emoji/CJK never break on unusual locales.
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
        print(md)


if __name__ == "__main__":
    main()
