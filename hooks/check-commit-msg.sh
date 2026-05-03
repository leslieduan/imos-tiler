#!/usr/bin/env bash
# Enforces conventional commit format:
#   type(scope): description
# where type is one of: feat fix docs style refactor test chore perf ci build revert
MSG=$(head -1 "$1")
PATTERN='^(feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert)(\([a-z0-9/_-]+\))?: .{1,72}$'

if echo "$MSG" | grep -qE "$PATTERN"; then
  exit 0
fi

echo "ERROR: commit message does not follow conventional commits format."
echo "  Expected: type(scope): description"
echo "  Types:    feat|fix|docs|style|refactor|test|chore|perf|ci|build|revert"
echo "  Got:      $MSG"
exit 1
