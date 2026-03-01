# 🤖 TF Plan Analyzer

LLM-powered Terraform plan risk assessment that posts human-readable analysis
as GitHub PR comments. Works with **GitHub Actions**, **Spacelift**, **Azure
DevOps** (via GitHub repos), or as a **standalone CLI tool**.

A developer who has never touched your Terraform code can look at the PR comment
and immediately understand what's about to happen, what the risks are, and
whether they should hit Apply.

## How It Works

```
terraform plan → JSON → Change Extraction → LLM Analysis → PR Comment
```

1. **Parse**: Reads `terraform show -json` output
2. **Extract**: Pulls out only the changed resources and computes attribute diffs
   (reduces token usage vs. sending the full plan)
3. **Analyze**: Sends the condensed changes to Claude with a risk-analysis prompt
4. **Post**: Writes the assessment as a GitHub PR comment (updates in-place on
   subsequent runs to avoid comment spam)

## Example Output

The PR comment will look like this:

> ## 🔍 Terraform Plan Analysis
>
> ### Risk Level: 🟡 MEDIUM
>
> ### 📋 Summary
> This plan modifies the OpenClaw-Main VM to shut it down and updates
> several metadata attributes.
>
> ### 📊 Change Breakdown
> | Action | Count | Resources |
> |--------|-------|-----------|
> | Update | 1     | proxmox_virtual_environment_vm.openclaw_main |
>
> ### ⚠️ Flagged Concerns
> - **VM will be shut down**: The `started` attribute is changing from
>   `true` → `false`, meaning OpenClaw-Main will be powered off on apply.
>
> ### ✅ Recommendation
> **Review recommended** — The VM shutdown may be intentional, but verify
> before applying. If this VM hosts services, ensure they've been migrated
> or that downtime is acceptable.
>
> ### 💬 Plain English
> Your main OpenClaw AI VM is going to be turned off. If that's what you
> intended, go ahead and apply. If not, check your Terraform config for the
> `started` attribute — it's currently set to `false`.

## Quick Start

### Local CLI

```bash
# Install
pip install anthropic requests

# Generate plan JSON
cd /your/terraform/dir
terraform plan -out=tfplan
terraform show -json tfplan > plan.json

# Run analysis (prints to terminal)
export ANTHROPIC_API_KEY="sk-ant-..."
python tf_plan_analyzer.py plan.json

# Or post to a PR
export GITHUB_TOKEN="ghp_..."
python tf_plan_analyzer.py plan.json \
  --github-comment \
  --pr 42 \
  --repo swift-innovate/tf_plan_analyzer
```

### GitHub Actions

1. Copy `tf_plan_analyzer.py` to your repo root
2. Copy `.github/workflows/tf-plan-analyze.yml` to your repo
3. Add `ANTHROPIC_API_KEY` as a repository secret
4. `GITHUB_TOKEN` is provided automatically by Actions

The workflow triggers on PRs that modify `.tf` or `.tfvars` files.

### Spacelift

1. Add `tf_plan_analyzer.py` and `spacelift-hook.sh` to your repo
2. In your Spacelift stack, add a **Before Apply** hook:
   ```
   bash spacelift-hook.sh
   ```
3. Set environment variables in Spacelift:
   - `ANTHROPIC_API_KEY` (secret)
   - `GITHUB_TOKEN` (secret)
   - `GITHUB_REPOSITORY` = `swift-innovate/tf_plan_analyzer`

### Azure DevOps (with GitHub repo)

If your ADO pipeline uses a GitHub repo, the GitHub Actions workflow handles
it automatically. If you're using ADO repos directly, add a pipeline task:

```yaml
- task: PythonScript@0
  displayName: "TF Plan Analyzer"
  inputs:
    scriptSource: filePath
    scriptPath: tf_plan_analyzer.py
    arguments: "$(System.DefaultWorkingDirectory)/plan.json --output analysis.md"
  env:
    ANTHROPIC_API_KEY: $(ANTHROPIC_API_KEY)

- task: PublishBuildArtifacts@1
  inputs:
    pathToPublish: analysis.md
    artifactName: tf-analysis
```

For ADO PR comments, swap the GitHub comment logic for the ADO REST API
(`POST /pullRequests/{id}/threads`). The LLM analysis is the same.

## Configuration

| Env Variable         | Description                           | Required |
|---------------------|---------------------------------------|----------|
| `ANTHROPIC_API_KEY` | Claude API key                        | Yes      |
| `GITHUB_TOKEN`      | GitHub token for PR comments          | For PR comments |
| `GITHUB_REPOSITORY` | `owner/repo` format                   | For PR comments |
| `PR_NUMBER`         | Pull request number                   | For PR comments |
| `TF_PLAN_JSON`      | Path to plan JSON (alt to CLI arg)    | No       |
| `TF_ANALYZER_MODEL` | Claude model (default: claude-sonnet-4-5-20250929) | No |

## CLI Options

```
usage: tf_plan_analyzer.py [-h] [--model MODEL] [--github-comment]
                            [--pr PR] [--repo REPO] [--output FILE]
                            [--json-summary] [plan_file]

Options:
  plan_file             Path to terraform plan JSON
  --model MODEL         LLM model to use
  --github-comment      Post as GitHub PR comment
  --pr PR               PR number
  --repo REPO           GitHub repo (owner/repo)
  --output, -o FILE     Write analysis to file
  --json-summary        Print condensed change summary (for debugging)
```

## Cost Estimate

Using Claude Sonnet, a typical plan analysis costs ~$0.01-0.05 per run
depending on plan size. For cost-sensitive CI environments, use
`--model claude-haiku-4-5-20241022` for ~10x cheaper runs with slightly
less detailed analysis.

## How the Risk Levels Work

| Level    | Trigger                                              |
|----------|------------------------------------------------------|
| 🟢 LOW      | Additions only, metadata/tag changes             |
| 🟡 MEDIUM   | In-place updates that change behavior            |
| 🔴 HIGH     | Destroys, replacements of stateful resources     |
| 🔴 CRITICAL | Multi-resource destruction, auth/security changes|

The analyzer also provides a **recommendation** (auto-approve, review,
manual review, or block) that maps well to OPA-style policy gates.

## License

MIT
