#!/usr/bin/env bash
# -----------------------------------------------
# spacelift-hook.sh
# -----------------------------------------------
# Drop this into your Spacelift stack as a
# "before apply" or "after plan" custom hook.
#
# Prerequisites:
#   - ANTHROPIC_API_KEY set as a Spacelift environment variable (secret)
#   - GITHUB_TOKEN set as a Spacelift environment variable (secret)
#   - Python 3.9+ available in the runner image
#
# Spacelift exposes these automatically:
#   - TF_VAR_spacelift_run_id
#   - SPACELIFT_COMMIT_SHA
#   - SPACELIFT_STACK_SLUG
# -----------------------------------------------

set -euo pipefail

echo "🤖 TF Plan Analyzer - Spacelift Hook"

# ── Install dependencies ──
pip install --quiet anthropic requests

# ── Generate plan JSON ──
# Spacelift stores the plan file; we just need to export it as JSON.
# The plan is available in the working directory after the plan phase.
if [ -f "spacelift.plan" ]; then
    terraform show -json spacelift.plan > plan.json
elif [ -f "plan.out" ]; then
    terraform show -json plan.out > plan.json
else
    echo "⚠️  No plan file found. Attempting to generate..."
    terraform plan -input=false -out=tfplan.tmp
    terraform show -json tfplan.tmp > plan.json
    rm -f tfplan.tmp
fi

# ── Determine PR number ──
# Spacelift tracks the commit; we look up the associated PR via GitHub API.
if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    PR_NUMBER=$(curl -s \
        -H "Authorization: Bearer ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github.v3+json" \
        "https://api.github.com/repos/${GITHUB_REPOSITORY}/commits/${SPACELIFT_COMMIT_SHA}/pulls" \
        | python3 -c "import sys,json; pulls=json.load(sys.stdin); print(pulls[0]['number'] if pulls else '')" \
        2>/dev/null || echo "")
fi

# ── Run the analyzer ──
ANALYZE_ARGS="plan.json"

if [ -n "${PR_NUMBER:-}" ] && [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    echo "📝 Will post analysis to PR #${PR_NUMBER}"
    ANALYZE_ARGS="${ANALYZE_ARGS} --github-comment --pr ${PR_NUMBER} --repo ${GITHUB_REPOSITORY}"
else
    echo "ℹ️  No PR detected or GitHub credentials missing. Printing to stdout only."
fi

python3 tf_plan_analyzer.py ${ANALYZE_ARGS}

echo "✅ TF Plan Analyzer complete"
