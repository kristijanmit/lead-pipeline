#!/usr/bin/env bash
# Block commits that would include sensitive files or pipeline run data.

if git diff --cached --name-only \
   | grep -qE '(^|/)\.env(\..*)?$|\.(key|pem)$|(^|/)secrets\.json$|(^|/)seen_domains\.json$|(^|/)creds\.md$|^data/'; then
  echo "BLOCKED: attempt to commit sensitive files or data/ contents"
  git diff --cached --name-only \
    | grep -E '(^|/)\.env(\..*)?$|\.(key|pem)$|(^|/)secrets\.json$|(^|/)seen_domains\.json$|(^|/)creds\.md$|^data/'
  exit 1
fi
