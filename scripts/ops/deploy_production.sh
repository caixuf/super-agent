#!/usr/bin/env bash
# ==============================================================================
# Production One-Click Deployment & Rollback Pipeline for Super Agent
# ==============================================================================
set -euo pipefail

REMOTE_HOST="${AGENTCORE_REMOTE_HOST:-remote-host}"
REMOTE_CODE_DIR="${AGENTCORE_REMOTE_DIR:-$HOME/code/agentcore}"
REMOTE_PROFILE_DIR="${AGENTCORE_PROFILE_DIR:-/mnt/nvme_ntfs/agents/hermes/profiles/remote}"
REMOTE_SERVICE="${AGENTCORE_SERVICE:-hermes-gateway-remote.service}"
LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "======================================================================"
echo " [Step 1/6] Running Local Pre-flight Verification & Neutrality Gates"
echo "======================================================================"
PYTHONPATH="$LOCAL_REPO" pytest "$LOCAL_REPO/tests/test_repo_neutrality.py"
PYTHONPATH="$LOCAL_REPO" python3 "$LOCAL_REPO/checks/verify_memory_source.py"

echo "======================================================================"
echo " [Step 2/6] Verifying Remote Host State (Read-Only)"
echo "======================================================================"
ssh "$REMOTE_HOST" "uptime && systemctl --user is-active $REMOTE_SERVICE"

echo "======================================================================"
echo " [Step 3/6] Generating Remote Atomic Backup Snapshot"
echo "======================================================================"
BACKUP_RES=$(ssh "$REMOTE_HOST" "python3 $REMOTE_CODE_DIR/scripts/ops/backup_and_rollback.py create --repo $REMOTE_CODE_DIR --config $REMOTE_PROFILE_DIR/config.yaml")
echo "$BACKUP_RES"
BACKUP_DIR=$(echo "$BACKUP_RES" | grep '"backup_dir"' | cut -d'"' -f4)
echo "Backup successfully preserved at: $BACKUP_DIR"

echo "======================================================================"
echo " [Step 4/6] Pushing Verified Master Branch to Remote"
echo "======================================================================"
git -C "$LOCAL_REPO" push "$REMOTE_HOST:$REMOTE_CODE_DIR" master

echo "======================================================================"
echo " [Step 5/6] Gracefully Draining Active Tasks & Reloading Remote Gateway"
echo "======================================================================"
ssh "$REMOTE_HOST" "python3 -m agentcore.liveness --drain --timeout 60 || true"
ssh "$REMOTE_HOST" "systemctl --user reload-or-restart $REMOTE_SERVICE"

echo "======================================================================"
echo " [Step 6/6] Post-Deployment Health Check & Gate Verification"
echo "======================================================================"
sleep 2
if ssh "$REMOTE_HOST" "systemctl --user is-active $REMOTE_SERVICE | grep -q active"; then
    echo "Gateway is active and healthy!"
    echo "Deployment successfully completed!"
else
    echo "ERROR: Remote gateway failed to enter active state! Initiating immediate rollback..."
    ssh "$REMOTE_HOST" "python3 $REMOTE_CODE_DIR/scripts/ops/backup_and_rollback.py rollback --backup-dir $BACKUP_DIR"
    ssh "$REMOTE_HOST" "systemctl --user restart $REMOTE_SERVICE"
    echo "Rollback completed."
    exit 1
fi
