#!/usr/bin/env bash
# 拆掉集群和本地镜像仓库,并清掉 demo-app 的本地 git 历史。
set -euo pipefail
cd "$(dirname "$0")/.."

kind delete cluster --name cicd
docker rm -f kind-registry >/dev/null 2>&1 || true
rm -rf .demo-git
