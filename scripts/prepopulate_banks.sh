#!/usr/bin/env bash
# Prepopulate ALL question banks found in the question_banks folder into the
# standalone enterprise-rag-core service's stores. Requires
# `enterprise-rag-core` on PATH (the same venv that runs `serve`).
# Idempotent: reruns skip unless --force is passed — a bank dropped into the
# folder is registered on the next run without editing this script.
#
#   scripts/prepopulate_banks.sh            # every question_banks/*.md
#   scripts/prepopulate_banks.sh --force    # rebuild from the corpus
set -euo pipefail
cd "$(dirname "$0")/.."

for kb in question_banks/*.md; do
  [ -e "$kb" ] || continue    # no matches: the glob stays literal
  name="$(basename "$kb" .md | tr 'A-Z' 'a-z')"
  case "$name" in
    ''|*[!a-z0-9-]*|-*)        # empty or not a slug (incl. leading dash)
      echo "skipping $kb: file name cannot be a skill (use lowercase letters, digits and dashes)" >&2
      continue ;;
  esac
  enterprise-rag-core prepopulate \
    --kb "$kb" \
    --doc-id "bank-$name" \
    --tenant "${RAG_CORE_DEFAULT_TENANT:-default}" \
    --department "$name" \
    "$@"
done
