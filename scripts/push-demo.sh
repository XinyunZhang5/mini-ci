#!/usr/bin/env bash
# 把 demo-app/ 的当前内容提交并 push 到集群内的 git 服务,然后发一个 webhook 触发流水线。
# demo-app 的 git 历史放在 .demo-git/ 里,和外层目录互不干扰。
set -euo pipefail
cd "$(dirname "$0")/.."

export GIT_DIR="$PWD/.demo-git" GIT_WORK_TREE="$PWD/demo-app"
REMOTE=git://localhost:19418/demo-app.git

[ -d "$GIT_DIR" ] || git init -q -b main
git add -A
if ! git diff --cached --quiet || ! git rev-parse -q --verify HEAD >/dev/null; then
  git -c user.name=mini-ci -c user.email=mini-ci@localhost commit -q -m "${1:-update demo-app}"
fi
git push -q "$REMOTE" main
SHA=$(git rev-parse HEAD)
echo "pushed $SHA"

python3 scripts/trigger.py "$SHA"
