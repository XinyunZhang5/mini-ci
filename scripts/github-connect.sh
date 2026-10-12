#!/usr/bin/env bash
# 把一个 GitHub 仓库接到本机的 mini-ci 上:
#   1. 把 gh 当前登录账号的 token 存进集群(operator 用它读 .ci.yaml、回写 commit status;
#      clone 步骤用它拉私有仓库)
#   2. 让 GitHub 把这个仓库的 push 事件转发到本机的 webhook(前台运行,Ctrl-C 停止)
#
# 用法: ./scripts/github-connect.sh <owner/repo>
# 前置: gh 已登录,且装了转发扩展: gh extension install cli/gh-webhook
set -euo pipefail

REPO="${1:?用法: $0 <owner/repo>}"
KCTL="kubectl --context kind-cicd -n ci"

$KCTL create secret generic github-token --from-literal=token="$(gh auth token)" \
  --dry-run=client -o yaml | $KCTL apply -f - >/dev/null
$KCTL rollout restart deployment/ci-operator >/dev/null
$KCTL rollout status deployment/ci-operator --timeout=120s >/dev/null
echo "token 已存入集群(secret/github-token),operator 已重启"

SECRET=$($KCTL get secret webhook-secret -o go-template='{{.data.secret | base64decode}}')
echo "正在把 $REPO 的 push 事件转发到 http://localhost:18080/webhook (Ctrl-C 停止)"
exec gh webhook forward --repo="$REPO" --events=push \
  --url=http://localhost:18080/webhook --secret="$SECRET"
