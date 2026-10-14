#!/usr/bin/env bash
# Fails when a change sets `verified: true` in a corpus case outside a corpus-record/ branch (ADR 0013).
# usage: check-corpus-verified.sh <base-ref> <head-branch-name>
set -euo pipefail
base="$1" branch="$2"
added=$(git diff --unified=0 "$base"...HEAD -- 'corpus/*.yaml' 'corpus/**/*.yaml' | grep -E '^\+verified:[[:space:]]*true' || true)
if [[ -n "$added" && "$branch" != corpus-record/* ]]; then
  echo "verified: true may only be set by the corpus-record workflow (branch corpus-record/*), not on '$branch'." >&2
  exit 1
fi
echo "corpus verified guard: ok"
