#!/usr/bin/env bash
# GitHub repo settings as config-as-code — not hand-clicked in the web UI.
# Idempotent: re-run any time to converge the remote repo settings to this spec.
# Each setting is applied independently and tolerates plan-tier limits (some
# private-repo protections need a paid plan — those are reported, not fatal).
# Re-run after upgrading the plan to pick them up.
#
# Usage:  ./scripts/harden-github.sh [owner/repo]
set -uo pipefail

REPO="${1:-kphutt/gdmutant}"
ok()   { printf '\033[1;32m[ok]\033[0m   %s\n' "$*"; }
warn() { printf '\033[1;33m[skip]\033[0m %s\n' "$*"; }
log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

log "Hardening ${REPO}"

# --- Merge hygiene: squash-only, auto-delete merged branches ---------------
if gh api -X PATCH "repos/${REPO}" \
     -F delete_branch_on_merge=true \
     -F allow_squash_merge=true \
     -F allow_merge_commit=false \
     -F allow_rebase_merge=false \
     -F allow_auto_merge=true >/dev/null 2>&1; then
  ok "Merge hygiene: squash-only, delete-branch-on-merge, auto-merge allowed."
else
  warn "Could not set merge hygiene (permissions?)."
fi

# --- Actions: default token read-only --------------------------------------
if gh api -X PUT "repos/${REPO}/actions/permissions/workflow" \
     -F default_workflow_permissions=read \
     -F can_approve_pull_request_reviews=false >/dev/null 2>&1; then
  ok "Actions default token set read-only."
else
  warn "Could not set Actions token permissions."
fi

# --- Dependabot alerts + automated security fixes --------------------------
gh api -X PUT "repos/${REPO}/vulnerability-alerts" >/dev/null 2>&1 \
  && ok "Dependabot vulnerability alerts enabled." \
  || warn "Could not enable vulnerability alerts."
gh api -X PUT "repos/${REPO}/automated-security-fixes" >/dev/null 2>&1 \
  && ok "Dependabot automated security fixes enabled." \
  || warn "Could not enable automated security fixes."

# --- Secret scanning + push protection (needs GHAS on private repos) -------
if gh api -X PATCH "repos/${REPO}" \
     --raw-field '{"security_and_analysis":{"secret_scanning":{"status":"enabled"},"secret_scanning_push_protection":{"status":"enabled"}}}' >/dev/null 2>&1; then
  ok "Native secret scanning + push protection enabled."
else
  warn "Secret scanning/push protection unavailable (private repos need GitHub Advanced Security). gitleaks in CI is the backstop."
fi

# --- Branch protection on main (needs a paid plan for PRIVATE repos) --------
# Require PR + the CI status checks, block force-push/deletion, admins included.
# Solo repo => required approving reviews = 0 (self-approval is impossible; a
# nonzero count would deadlock). CODEOWNERS review stays advisory.
read -r -d '' PROTECTION <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Verify", "Secret scan (gitleaks)"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
if echo "$PROTECTION" | gh api -X PUT "repos/${REPO}/branches/main/protection" \
     -H "Accept: application/vnd.github+json" --input - >/dev/null 2>&1; then
  ok "Branch protection on 'main': PR required, CI checks required, no force-push."
else
  warn "Branch protection NOT set — private-repo protection needs a paid GitHub plan."
  warn "  Until then, PR discipline is convention-only. Upgrade, then re-run this script."
fi

log "Done. Review live settings:  gh api repos/${REPO} --jq '{private,delete_branch_on_merge}'"
