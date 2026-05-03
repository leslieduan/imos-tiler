#!/usr/bin/env bash
# Enforces branch naming: main  or  type/description
# e.g. feat/add-manifest-endpoint, fix/lod-off-by-one
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null)
PATTERN='^(main|(feat|fix|chore|docs|refactor|test|ci|perf|revert)/.+)$'

if echo "$BRANCH" | grep -qE "$PATTERN"; then
  exit 0
fi

echo "ERROR: branch name '$BRANCH' does not follow naming convention."
echo "  Expected: main  or  type/description"
echo "  Types:    feat|fix|chore|docs|refactor|test|ci|perf|revert"
echo "  Example:  feat/add-manifest-endpoint"
exit 1
