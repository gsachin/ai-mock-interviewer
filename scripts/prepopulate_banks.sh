#!/usr/bin/env bash
# Prepopulate all domain question banks into the standalone enterprise-rag-core
# service's stores. Requires `enterprise-rag-core` on PATH (the same venv that
# runs `serve`). Idempotent: reruns skip unless --force is passed.
#
#   scripts/prepopulate_banks.sh            # all four domains
#   scripts/prepopulate_banks.sh --force    # rebuild from the corpus
set -euo pipefail
cd "$(dirname "$0")/.."

for domain in system-design ios dsa devops; do
  enterprise-rag-core prepopulate \
    --kb "question_banks/$domain.md" \
    --doc-id "bank-$domain" \
    --tenant "${RAG_CORE_DEFAULT_TENANT:-default}" \
    --department "$domain" \
    "$@"
done
