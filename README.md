# claude-chat-to-md

Convert [Claude Code](https://docs.anthropic.com/en/docs/claude-code) chat sessions to clean, readable Markdown — including subagent conversations.

Claude Code persists full chat history as JSONL files under `~/.claude/projects/`. This tool reads those files and produces well-formatted Markdown with proper headings, code blocks, unified diffs, and collapsible sections for tool results and subagent conversations.

**Zero runtime dependencies** — just the Python standard library. **Linux-only**, but the generated `.md` files are UTF-8 and open cleanly on both Linux and Windows.

## Install

Install straight from GitHub with `pipx` (recommended) or `pip`:

```bash
pipx install git+https://github.com/civisrom/claude-chat-to-md.git
```

```bash
pip install git+https://github.com/civisrom/claude-chat-to-md.git
```

Or from a clone for development:

```bash
git clone https://github.com/civisrom/claude-chat-to-md.git
cd claude-chat-to-md
pip install -e .
```

To upgrade later:

```bash
pip install --upgrade git+https://github.com/civisrom/claude-chat-to-md.git
```

## Usage

### List sessions

```bash
claude-chat-to-md --list
```

```
#    Date                 ID           Title                                              Project
------------------------------------------------------------------------------------------------------------------------
1    2026-04-11 14:30     a1b2c3d4..   Refactor auth middleware                            dev/myapp
2    2026-04-10 09:15     e5f6a7b8..   Add user settings page                              dev/myapp
3    2026-04-09 16:45     c9d0e1f2..   Debug CI pipeline                                   dev/infra
```

### Convert a session

```bash
# By index (from --list)
claude-chat-to-md 1 -o chat.md

# By UUID prefix
claude-chat-to-md a1b2c3 -o chat.md

# By title substring
claude-chat-to-md "auth middleware" -o chat.md

# Most recent session
claude-chat-to-md --latest -o chat.md
```

### Filter by project

```bash
claude-chat-to-md --list --project myapp
claude-chat-to-md --latest --project myapp -o chat.md
```

### Export all sessions

```bash
claude-chat-to-md --all --output-dir ./exports/
```

Output filenames are built as `YYYY-MM-DD-title-<sid>.md` and sanitized to be safe on both Linux and Windows filesystems (no `<>:"/\|?*`, no reserved names like `CON`/`PRN`, collision-free via 8-char session-id suffix).

### Options

| Flag | Description |
|---|---|
| `--list`, `-l` | List all available sessions |
| `--latest` | Convert the most recent session |
| `--all` | Convert all sessions |
| `--project`, `-p` | Filter sessions by project path substring |
| `--output`, `-o` | Output file (default: stdout) |
| `--output-dir`, `-d` | Output directory for `--all` mode |
| `--no-subagents` | Exclude subagent conversations |
| `--no-tool-results` | Exclude tool call results |

## Shell aliases (zsh)

Convenience wrappers for interactive use — drop into your `~/.zshrc`. Adjust `/root/skripts/bin/python3` to match the Python interpreter where you installed the package.

```zsh
# --- claude-chat-to-md -------------------------------------------------------
# Internal helper: full path to the installed tool.
_claude_chat() { /root/skripts/bin/python3 -m claude_chat_to_md "$@" }

# Direct pass-through: chat --help / chat <uuid> / chat --list --project foo
alias chat='_claude_chat'

# Listing
alias chat-list='_claude_chat --list'

# Quick exports (without tool-results for compact output)
alias chat-latest='_claude_chat --latest --no-tool-results -o ~/chat-latest.md && echo "Saved: ~/chat-latest.md"'
alias chat-latest-full='_claude_chat --latest -o ~/chat-latest-full.md && echo "Saved: ~/chat-latest-full.md"'
alias chat-all='_claude_chat --all --no-tool-results --output-dir ~/chats/ && echo "Saved to: ~/chats/"'
alias chat-all-full='_claude_chat --all --output-dir ~/chats-full/ && echo "Saved to: ~/chats-full/"'

# Save one session (compact): chat-save <idx|uuid> [filename]
chat-save() {
    local q="${1:?Usage: chat-save <index|uuid> [filename]}"
    local name="${2:-chat-$(date +%Y%m%d-%H%M%S)}"
    _claude_chat "$q" --no-tool-results -o ~/"${name}.md" \
        && echo "Saved: ~/${name}.md"
}

# Save one session with all tool-results: chat-save-full <idx|uuid> [filename]
chat-save-full() {
    local q="${1:?Usage: chat-save-full <index|uuid> [filename]}"
    local name="${2:-chat-full-$(date +%Y%m%d-%H%M%S)}"
    _claude_chat "$q" -o ~/"${name}.md" \
        && echo "Saved: ~/${name}.md"
}

# View a session in less: chat-view <idx|uuid>
chat-view() {
    local q="${1:?Usage: chat-view <index|uuid>}"
    _claude_chat "$q" --no-tool-results | less -R
}

# Open a session in $EDITOR (temp file): chat-edit <idx|uuid>
chat-edit() {
    local q="${1:?Usage: chat-edit <index|uuid>}"
    local tmp
    tmp="$(mktemp --suffix=.md)" || return 1
    _claude_chat "$q" --no-tool-results -o "$tmp" && "${EDITOR:-nano}" "$tmp"
    rm -f "$tmp"
}

# Filter by project: chat-project <project-substring>
chat-project() {
    local proj="${1:?Usage: chat-project <project-substring>}"
    _claude_chat --list --project "$proj"
}

# Save latest session for a project: chat-project-latest <project> [filename]
chat-project-latest() {
    local proj="${1:?Usage: chat-project-latest <project> [filename]}"
    local name="${2:-chat-${proj}-$(date +%Y%m%d-%H%M%S)}"
    _claude_chat --latest --project "$proj" --no-tool-results -o ~/"${name}.md" \
        && echo "Saved: ~/${name}.md"
}

# Search sessions by title substring: chat-find <substring>
chat-find() {
    local q="${1:?Usage: chat-find <substring>}"
    _claude_chat --list | grep -i --color=auto -- "$q"
}

# Interactive picker via fzf: chat-fzf
chat-fzf() {
    command -v fzf >/dev/null || { echo "fzf is not installed"; return 1; }
    local idx
    idx=$(_claude_chat --list | tail -n +3 | fzf --height=60% --reverse --prompt='session> ' | awk '{print $1}')
    [[ -n "$idx" ]] && _claude_chat "$idx" --no-tool-results | less -R
}

# Upgrade the installed package from GitHub
alias chat-update='/root/skripts/bin/pip install --upgrade git+https://github.com/luckynick/claude-chat-to-md.git'

# Clean up old exports: keep only the 20 most recent .md files in ~/chats/
chat-clean() {
    local dir="${1:-$HOME/chats}"
    [[ -d "$dir" ]] || { echo "No such directory: $dir"; return 1; }
    ls -t "$dir"/*.md 2>/dev/null | tail -n +21 | xargs -r rm -v
}
```

### Alias reference

| Command | What it does |
|---|---|
| `chat [args...]` | Direct pass-through to the CLI (e.g. `chat --help`, `chat <uuid>`) |
| `chat-list` | List all sessions (#, date, ID, title, project) |
| `chat-latest` | Save latest session → `~/chat-latest.md` (compact, overwrites) |
| `chat-latest-full` | Same, but with all tool-results → `~/chat-latest-full.md` |
| `chat-all` | Export every session → `~/chats/` (compact) |
| `chat-all-full` | Export every session with full tool-results → `~/chats-full/` |
| `chat-save <N> [name]` | Save session by index / UUID / title substring |
| `chat-save-full <N> [name]` | Same, but keep all tool-results |
| `chat-view <N>` | Read session in `less -R` without saving |
| `chat-edit <N>` | Open session in `$EDITOR` via temp file (cleaned up on exit) |
| `chat-project <name>` | List sessions filtered by project substring |
| `chat-project-latest <name> [file]` | Save latest session of a specific project |
| `chat-find <text>` | Grep `chat-list` output by title with colored matches |
| `chat-fzf` | Interactive session picker via `fzf` → view in `less` |
| `chat-update` | `pip install --upgrade git+https://github.com/luckynick/claude-chat-to-md.git` |
| `chat-clean [dir]` | Keep only the 20 most recent `.md` exports (default `~/chats/`) |

Reload the config after adding the block:

```bash
source ~/.zshrc
```

With `COMPLETE_ALIASES` enabled (already set in [civisrom/debian-ubuntu-setup](https://github.com/civisrom/debian-ubuntu-setup/blob/main/config/.zshrc)), tab-completion works for all `chat-*` commands.

## Output format

- **User messages** → `## User` sections
- **Assistant messages** → `## Assistant` sections with text and tool calls
- **Tool results** → collapsible `<details>` blocks
- **Subagent conversations** → collapsible `<details>` blocks linked to the parent Agent call via `tool_use_id`
- **`Edit` / `MultiEdit`** → proper unified-diff code blocks (via `difflib`)
- **`Bash`** / **`Write`** → fenced code blocks with automatic fence-length adjustment so embedded backticks never break the block
- **`TodoWrite`** → human-readable checklists (`[x]` / `[~]` / `[ ]`)
- **`NotebookEdit`** → shows mode, cell id, new source
- **`WebFetch`** → url + truncated prompt
- **Code** → fenced with language hints sniffed from file extension
- System and IDE tags are stripped: `ide_opened_file`, `ide_selection`, `system-reminder`, `command-*`, `local-command-*`, `user-prompt-submit-hook`, `user-memory-input`

## How it works

Claude Code stores sessions at:

```
~/.claude/projects/<encoded-project-path>/<session-uuid>.jsonl
```

Each line is a JSON object: `user` messages, `assistant` messages (with `tool_use` blocks), `tool_result` responses, metadata, and session summaries. Subagent conversations live in a `subagents/` subdirectory alongside the main session, with a `.jsonl` + `.meta.json` pair per subagent. The tool links each subagent to its spawning `Agent` / `Task` call by `tool_use_id` when available, falling back to a description match.

## Privacy

The tool is strictly local. No network calls, no telemetry, no subprocess execution, no third-party dependencies. Session data never leaves your machine — inputs are read from `~/.claude/projects/`, outputs go only to stdout or a path you specify.

## Requirements

- Linux
- Python 3.10+
- No external runtime dependencies
