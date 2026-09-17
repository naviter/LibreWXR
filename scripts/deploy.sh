#!/usr/bin/env bash
# Deploy the naviter build of LibreWXR to librewxr.seeyou.cloud (h03 CT 115).
# A laptop and CI (.github/workflows/deploy.yml) both run this script, so both
# deploy the same way with the same settings (deploy/librewxr.env).
#
#   scripts/deploy.sh            deploy the checked-out commit
#   scripts/deploy.sh --force    deploy even if the deploy lock refuses (or FORCE=true)
#
# The box is a git checkout (/srv/librewxr) with an editable install in
# .venv312, run as two systemd units in multi mode: librewxr-pipeline (fetch)
# and librewxr-render (tiles). A deploy moves that checkout to this commit, so
# the commit must be pushed and the working tree clean. The box's .env holds
# only the secrets; everything else is deploy/librewxr.env.
set -euo pipefail

SSH_DESTINATION=root@135.181.120.42
SSH_PORT=22115
REMOTE_DIR=/srv/librewxr
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FORCE=${FORCE:-false}
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# ── Deploy guard: lock.sh on the api box refuses a deploy that would replace
# someone's work (another branch, their uncommitted changes, newer commits).
# FORCE=true or --force deploys anyway. See naviter/navigator scripts/publish/lock.sh.
SERVICE=librewxr
BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)
COMMIT=$(git -C "$REPO_DIR" rev-parse HEAD)
if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  SOURCE=ci; DEPLOYER=$GITHUB_ACTOR; DIRTY=false
else
  SOURCE=local; DEPLOYER=$(git -C "$REPO_DIR" config user.email || whoami)
  DIRTY=$([ -n "$(git -C "$REPO_DIR" status --porcelain)" ] && echo true || echo false)
fi
echo "lock    service=$SERVICE source=$SOURCE deployer=$DEPLOYER force=$FORCE"
echo "        branch=$BRANCH commit=$COMMIT dirty=$DIRTY"

# The box checks out a pushed commit, so there is no way to ship local edits.
if [[ "$DIRTY" == true ]]; then
  echo "✗ Uncommitted changes: the box deploys a pushed commit. Commit and push first." >&2
  exit 1
fi
if [[ "$BRANCH" == HEAD ]]; then
  echo "✗ Detached HEAD: check out the branch to deploy." >&2
  exit 1
fi

LOCK_ARGS=$(printf '%q ' service="$SERVICE" source="$SOURCE" deployer="$DEPLOYER" branch="$BRANCH" commit="$COMMIT" dirty="$DIRTY" force="$FORCE")
ssh -p 22116 root@h02.naviter.com "lock.sh read $LOCK_ARGS" || exit $?

echo "==> deploying $BRANCH@${COMMIT:0:9} to $SSH_DESTINATION:$SSH_PORT"
ssh -p "$SSH_PORT" "$SSH_DESTINATION" bash -s -- "$(printf '%q' "$REMOTE_DIR")" "$(printf '%q' "$BRANCH")" "$COMMIT" <<'REMOTE'
set -euo pipefail
dir=$1 branch=$2 commit=$3
cd "$dir"

# Tracked files only: .venv312 and the .env backups are untracked on purpose.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "✗ $dir has local modifications — refusing to deploy over them:" >&2
  git status --short --untracked-files=no >&2
  exit 1
fi
[ -f .env ] || { echo "✗ $dir/.env (the EUMETSAT secrets) is missing" >&2; exit 1; }

git fetch --quiet origin
git cat-file -e "$commit^{commit}" 2>/dev/null || {
  echo "✗ $commit is not on GitHub — push it first" >&2; exit 1;
}
previous=$(git rev-parse HEAD)
git checkout --quiet -B "$branch" "$commit"
echo "    checkout: ${previous:0:9} -> ${commit:0:9}"

# Editable install: only a dependency change needs pip.
if ! git diff --quiet "$previous" "$commit" -- pyproject.toml; then
  echo "    pyproject.toml changed — reinstalling"
  .venv312/bin/pip install --quiet -e .
fi

# Units and the nofile drop-in come from the checkout.
changed=false
for unit in librewxr-pipeline.service librewxr-render.service; do
  if ! cmp -s "deploy/systemd/$unit" "/etc/systemd/system/$unit"; then
    install -m 644 "deploy/systemd/$unit" "/etc/systemd/system/$unit"; changed=true
  fi
  mkdir -p "/etc/systemd/system/$unit.d"
  if ! cmp -s deploy/systemd/nofile.conf "/etc/systemd/system/$unit.d/nofile.conf"; then
    install -m 644 deploy/systemd/nofile.conf "/etc/systemd/system/$unit.d/nofile.conf"; changed=true
  fi
done
if $changed; then echo "    systemd units updated"; systemctl daemon-reload; fi

systemctl restart librewxr-pipeline
systemctl restart librewxr-render

# Render workers wait for the pipeline's state.json before serving.
for i in $(seq 1 60); do
  if curl -sf -o /dev/null http://127.0.0.1:8090/health; then
    echo "    healthy after $(( (i - 1) * 5 ))s"
    exit 0
  fi
  sleep 5
done
echo "✗ not healthy after 300s" >&2
journalctl -u librewxr-pipeline -u librewxr-render -n 30 --no-pager >&2
exit 1
REMOTE

ssh -p 22116 root@h02.naviter.com "lock.sh write $LOCK_ARGS" || echo "⚠ deploy not recorded"
echo "done — https://librewxr.seeyou.cloud"
