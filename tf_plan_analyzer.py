#!/usr/bin/env python3
"""
TF Plan Analyzer - LLM-Powered Terraform Plan Risk Assessment
=============================================================
Parses `terraform show -json <planfile>` output, sends it to an LLM
for human-readable risk analysis, and optionally posts the result
as a GitHub PR comment.

Works in:
  - GitHub Actions (for Azure DevOps via GitHub repos)
  - Spacelift (via before_apply hooks)
  - Local CLI usage

Environment Variables:
  ANTHROPIC_API_KEY    - API key for Claude
  GITHUB_TOKEN         - GitHub token for PR comments
  GITHUB_REPOSITORY    - owner/repo (auto-set in GitHub Actions)
  PR_NUMBER            - Pull request number to comment on
  TF_PLAN_JSON         - Path to plan JSON file (alternative to CLI arg)
"""

import json
import sys
import os
import re
import argparse
import textwrap
from datetime import datetime, timezone

try:
    import anthropic
except ImportError:
    print("ERROR: 'anthropic' package not installed. Run: pip install anthropic")
    sys.exit(1)

try:
    import requests
except ImportError:
    requests = None  # Optional - only needed for PR comments


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RISK_LEVELS = {
    "critical": "🔴 CRITICAL",
    "high":     "🔴 HIGH",
    "medium":   "🟡 MEDIUM",
    "low":      "🟢 LOW",
    "info":     "ℹ️  INFO",
}

SYSTEM_PROMPT = textwrap.dedent("""\
You are a Terraform plan risk analyst. Your job is to review Terraform plan
JSON and produce a clear, human-readable risk assessment that a developer
can quickly scan before clicking "Apply".

Analyze the plan and produce a report with EXACTLY this markdown structure:

## 🔍 Terraform Plan Analysis

### Risk Level: <EMOJI> <LEVEL>

Use one of: 🟢 LOW | 🟡 MEDIUM | 🔴 HIGH | 🔴 CRITICAL

Risk level criteria:
- **LOW**: Only additions or non-destructive updates (tags, labels, metadata).
- **MEDIUM**: In-place updates that change operational behavior (ports, IPs,
  sizes, configs, startup state). Also any resource replacements of
  non-critical/stateless resources.
- **HIGH**: Resource destruction or replacement of stateful resources (VMs,
  databases, volumes). Changes to IAM, networking, or security groups.
- **CRITICAL**: Destruction of multiple production resources, changes to
  auth/encryption settings, or modifications that could cause data loss.

### 📋 Summary

One or two sentences: what this plan does in plain English. A developer who
has never seen this Terraform code should understand what is about to happen.

### 📊 Change Breakdown

| Action | Count | Resources |
|--------|-------|-----------|
| Create | N     | list      |
| Update | N     | list      |
| Delete | N     | list      |
| Replace| N     | list      |

### ⚠️ Flagged Concerns

Bullet list of specific things the reviewer should pay attention to.
Each item should explain WHAT is changing and WHY it matters.
If a resource is being stopped, destroyed, or replaced — call it out clearly.
If security-relevant attributes change (firewall rules, auth, encryption,
public access) — call those out.
If there are no concerns, say "No concerns identified."

### ✅ Recommendation

One of:
- **Auto-approve** — Safe, additive-only changes.
- **Review recommended** — Behavioral changes worth a second look.
- **Manual review required** — Destructive or security-impacting changes.
  Explain what specifically needs human verification.
- **BLOCK — Do not apply** — High risk of data loss or outage. Explain why.

### 💬 Plain English

Explain every change as if talking to a developer who doesn't know Terraform.
Use simple language. For each resource being modified, explain:
- What it is (in real terms — "your main AI VM", not "proxmox_virtual_environment_vm.openclaw_main")
- What's changing about it
- What the practical impact is (will it reboot? lose data? go offline?)

Keep this section conversational and scannable.

Rules:
- Be specific. Reference actual resource names and attribute values from the plan.
- Do NOT hallucinate resources or changes not present in the plan.
- If the plan JSON is empty or has no changes, say so clearly.
- When a VM's `started` attribute changes to false, flag it prominently —
  that means the VM will be shut down.
- Pay special attention to: destroy actions, replace actions, changes to
  security groups / firewall rules, changes to disk/volume resources,
  changes to IAM or access controls, and changes to encryption settings.
""")

# ---------------------------------------------------------------------------
# Plan Parsing (pre-processing to reduce token usage)
# ---------------------------------------------------------------------------

def parse_plan_json(plan_path: str) -> dict:
    """Load and validate terraform plan JSON."""
    with open(plan_path, "r") as f:
        plan = json.load(f)

    if "resource_changes" not in plan:
        print("WARNING: No 'resource_changes' found. Is this `terraform show -json` output?")

    return plan


def extract_changes_summary(plan: dict) -> dict:
    """
    Extract a condensed summary of changes from the full plan JSON.
    This reduces token count significantly while preserving all
    decision-relevant information.
    """
    changes = []
    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])

        # Skip no-ops
        if actions == ["no-op"] or actions == ["read"]:
            continue

        change_detail = {
            "address": rc.get("address", "unknown"),
            "type": rc.get("type", "unknown"),
            "name": rc.get("name", "unknown"),
            "provider": rc.get("provider_name", "unknown"),
            "actions": actions,
        }

        # For updates, compute the actual diff (before vs after)
        before = rc.get("change", {}).get("before") or {}
        after = rc.get("change", {}).get("after") or {}

        if "update" in actions or "create" in actions or "delete" in actions:
            diff = {}
            all_keys = set(list(before.keys()) + list(after.keys()))
            for key in all_keys:
                bval = before.get(key)
                aval = after.get(key)
                if bval != aval:
                    # Skip large/noisy fields
                    if isinstance(bval, (list, dict)) and len(str(bval)) > 500:
                        bval = f"[{type(bval).__name__} - truncated]"
                    if isinstance(aval, (list, dict)) and len(str(aval)) > 500:
                        aval = f"[{type(aval).__name__} - truncated]"
                    diff[key] = {"before": bval, "after": aval}

            if diff:
                change_detail["attribute_changes"] = diff

        changes.append(change_detail)

    # Output / resource counts
    outputs_changed = len(plan.get("output_changes", {}))

    summary = {
        "terraform_version": plan.get("terraform_version", "unknown"),
        "format_version": plan.get("format_version", "unknown"),
        "total_resource_changes": len(changes),
        "resource_changes": changes,
        "outputs_changed": outputs_changed,
    }

    return summary


# ---------------------------------------------------------------------------
# LLM Analysis
# ---------------------------------------------------------------------------

def analyze_with_llm(changes_summary: dict, model: str = "claude-sonnet-4-5-20250929") -> str:
    """Send the condensed plan to Claude for analysis."""
    client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY env var

    plan_text = json.dumps(changes_summary, indent=2, default=str)

    # Token budget guard
    if len(plan_text) > 100_000:
        print("WARNING: Plan summary is very large. Truncating to avoid token limits.")
        plan_text = plan_text[:100_000] + "\n... [TRUNCATED]"

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Analyze this Terraform plan and provide a risk assessment.\n\n"
                    f"```json\n{plan_text}\n```"
                ),
            }
        ],
    )

    return message.content[0].text


# ---------------------------------------------------------------------------
# GitHub PR Comment
# ---------------------------------------------------------------------------

def post_github_comment(analysis: str, repo: str, pr_number: int, token: str):
    """Post the analysis as a PR comment on GitHub."""
    if requests is None:
        print("ERROR: 'requests' package not installed. Run: pip install requests")
        print("Skipping PR comment.")
        return False

    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    body = (
        f"# 🤖 TF Plan Analyzer\n\n"
        f"{analysis}\n\n"
        f"---\n"
        f"*Generated by TF Plan Analyzer at {timestamp}*"
    )

    # Check for existing bot comments and update instead of creating new
    existing = _find_existing_comment(url, headers)
    if existing:
        update_url = f"https://api.github.com/repos/{repo}/issues/comments/{existing}"
        resp = requests.patch(update_url, json={"body": body}, headers=headers)
    else:
        resp = requests.post(url, json={"body": body}, headers=headers)

    if resp.status_code in (200, 201):
        print(f"✅ Analysis posted to PR #{pr_number}")
        return True
    else:
        print(f"❌ Failed to post comment: {resp.status_code} - {resp.text}")
        return False


def _find_existing_comment(url: str, headers: dict) -> int | None:
    """Find an existing TF Plan Analyzer comment to update."""
    if requests is None:
        return None
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        for comment in resp.json():
            if "🤖 TF Plan Analyzer" in comment.get("body", ""):
                return comment["id"]
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze Terraform plan JSON with an LLM and produce a risk assessment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Local analysis (prints to stdout)
              terraform show -json tfplan > plan.json
              python tf_plan_analyzer.py plan.json

              # Post to GitHub PR
              python tf_plan_analyzer.py plan.json --github-comment --pr 42

              # Use a specific model
              python tf_plan_analyzer.py plan.json --model claude-haiku-4-5-20241022
        """),
    )

    parser.add_argument(
        "plan_file",
        nargs="?",
        default=os.environ.get("TF_PLAN_JSON"),
        help="Path to terraform plan JSON (from `terraform show -json <planfile>`)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("TF_ANALYZER_MODEL", "claude-sonnet-4-5-20250929"),
        help="LLM model to use (default: claude-sonnet-4-5-20250929)",
    )
    parser.add_argument(
        "--github-comment",
        action="store_true",
        help="Post analysis as a GitHub PR comment",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=os.environ.get("PR_NUMBER"),
        help="PR number to comment on (or set PR_NUMBER env var)",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="GitHub repo as owner/repo (or set GITHUB_REPOSITORY env var)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write analysis to file instead of stdout",
    )
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="Also print the condensed change summary (useful for debugging)",
    )

    args = parser.parse_args()

    if not args.plan_file:
        parser.error("No plan file specified. Provide a path or set TF_PLAN_JSON env var.")

    # --- Parse ---
    print(f"📖 Reading plan from: {args.plan_file}")
    plan = parse_plan_json(args.plan_file)

    # --- Extract ---
    print("🔬 Extracting change summary...")
    summary = extract_changes_summary(plan)

    if args.json_summary:
        print("\n--- Change Summary ---")
        print(json.dumps(summary, indent=2, default=str))
        print("--- End Summary ---\n")

    if summary["total_resource_changes"] == 0:
        analysis = (
            "## 🔍 Terraform Plan Analysis\n\n"
            "### Risk Level: ℹ️  INFO\n\n"
            "### 📋 Summary\n\n"
            "No resource changes detected in this plan. Nothing to apply.\n\n"
            "### ✅ Recommendation\n\n"
            "**Auto-approve** — No changes."
        )
        print("ℹ️  No resource changes found in plan.")
    else:
        print(f"🤖 Analyzing {summary['total_resource_changes']} resource change(s) with {args.model}...")
        analysis = analyze_with_llm(summary, model=args.model)

    # --- Output ---
    if args.output:
        with open(args.output, "w") as f:
            f.write(analysis)
        print(f"💾 Analysis written to: {args.output}")
    else:
        print("\n" + "=" * 70)
        print(analysis)
        print("=" * 70)

    # --- GitHub Comment ---
    if args.github_comment:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            print("❌ GITHUB_TOKEN not set. Skipping PR comment.")
            sys.exit(1)
        if not args.pr:
            print("❌ PR number not specified. Use --pr or set PR_NUMBER env var.")
            sys.exit(1)
        if not args.repo:
            print("❌ Repository not specified. Use --repo or set GITHUB_REPOSITORY env var.")
            sys.exit(1)

        post_github_comment(analysis, args.repo, int(args.pr), token)


if __name__ == "__main__":
    main()
