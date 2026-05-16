#!/usr/bin/env bash
# Stop hook: nudge Claude to consider doc updates when load-bearing files
# were edited but neither CLAUDE.md nor docs/technical.md were touched.
# Fires at most once per session.

set -euo pipefail

INPUT=$(cat)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty')
[ -z "$SESSION_ID" ] && exit 0

SENTINEL="/tmp/claude-doc-nudge-${SESSION_ID}"
[ -f "$SENTINEL" ] && exit 0

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$ROOT"

DIRTY=$(git diff HEAD --name-only 2>/dev/null) || exit 0
[ -z "$DIRTY" ] && exit 0

LOAD_BEARING_RE='^(constants\.py|services/(loader|data_renderer|visual_renderer)\.py|utils/dates\.py|routers/(products|data_tiles|visual_tiles)\.py)$'
DOCS_RE='^(CLAUDE\.md|docs/technical\.md)$'

LOAD_BEARING=$(printf '%s\n' "$DIRTY" | grep -E "$LOAD_BEARING_RE" || true)
DOCS=$(printf '%s\n' "$DIRTY" | grep -E "$DOCS_RE" || true)

if [ -n "$LOAD_BEARING" ] && [ -z "$DOCS" ]; then
  touch "$SENTINEL"
  FILES=$(printf '%s' "$LOAD_BEARING" | tr '\n' ' ' | sed 's/ *$//')
  jq -nc --arg files "$FILES" '{
    decision: "block",
    reason: ("Load-bearing files were edited (" + $files + ") but CLAUDE.md and docs/technical.md were not. Review whether the invariants or architecture notes there need updates. If they do not, briefly say why and stop again — this nudge fires once per session.")
  }'
fi
