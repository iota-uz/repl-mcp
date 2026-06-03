#!/usr/bin/env bash
# PostToolUse nudge: when Claude runs inline Python through Bash
# (python3 -c, python3 - <<EOF heredocs, or `cmd | python3`), remind it that
# the python-repl MCP tool exists. Non-blocking — the command already ran.
#
# Must NOT fire on: `python3 script.py`, `python3 -m pytest`, `pytest`, etc.
set -euo pipefail

input=$(cat)

# Extract tool_input.command (python3 is guaranteed present for this hook's purpose;
# if extraction fails, stay silent)
cmd=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("command", ""))
except Exception:
    pass
' 2>/dev/null) || exit 0

[ -z "$cmd" ] && exit 0

# Fire only on inline-python shapes (anchored on -c / stdin dash / heredoc / pipe-into-python):
#   python3 -c "..."          python -c "..."
#   python3 - <<'EOF'         python3 <<'EOF'
#   cmd | python3 -...        cmd | python3   (end of line)
if printf '%s' "$cmd" | grep -Eq \
  -e 'python3?[[:space:]]+-c[[:space:]]' \
  -e 'python3?[[:space:]]+-[[:space:]]*<<' \
  -e 'python3?[[:space:]]*<<' \
  -e '\|[[:space:]]*python3?[[:space:]]+-' \
  -e '\|[[:space:]]*python3?[[:space:]]*$'; then
  cat <<'JSON'
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "Reminder: prefer the python-repl MCP tool (execute_python) over inline python3 via Bash. It is a warm persistent REPL (~0.1s vs ~3s per spawn), keeps variables/imports across calls, has sh() for shell composition — e.g. json.loads(sh('gh pr view 1 --json title')) — and full filesystem access (open(), absolute paths, ~). If the tool is deferred, load it once with ToolSearch: select:mcp__python-repl__execute_python"}}
JSON
fi

exit 0
