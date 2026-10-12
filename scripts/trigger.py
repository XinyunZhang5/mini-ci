#!/usr/bin/env python3
"""模拟 GitHub 发一个带签名的 push webhook,然后跟到流水线结束。

用法: trigger.py <commit-sha> [--no-wait]
"""

import hashlib
import hmac
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONTROLLER = "http://localhost:18080"
KUBECTL = ["kubectl", "--context", "kind-cicd", "-n", "ci"]
# 构建 Pod 在集群里 clone,所以给的是集群内地址
CLONE_URL = "git://git-server.ci.svc.cluster.local:9418/demo-app.git"
WAIT_TIMEOUT = 1200


def webhook_secret() -> bytes:
    out = subprocess.check_output(
        [*KUBECTL, "get", "secret", "webhook-secret", "-o", "go-template={{.data.secret | base64decode}}"]
    )
    return out.strip()


def get(path: str) -> bytes:
    with urllib.request.urlopen(CONTROLLER + path) as resp:
        return resp.read()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sha = sys.argv[1]
    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "after": sha,
            "repository": {"name": "demo-app", "clone_url": CLONE_URL},
        }
    ).encode()
    signature = "sha256=" + hmac.new(webhook_secret(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        CONTROLLER + "/webhook",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": signature,
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"控制器拒绝了这次 push ({exc.code}): {exc.read().decode()}")
        return 1
    if "run" not in result:
        print(result["msg"])
        return 0
    run = result["run"]
    print(f"PipelineRun {run} 已创建")
    if "--no-wait" in sys.argv:
        return 0

    shown = None
    deadline = time.monotonic() + WAIT_TIMEOUT
    while True:
        time.sleep(2)
        info = json.loads(get(f"/runs/{run}"))
        if time.monotonic() > deadline:
            print(f"等了 {WAIT_TIMEOUT}s 还没结束(phase={info['phase']}),operator 在跑吗?")
            return 1
        if info["currentStep"] and info["currentStep"] != shown:
            shown = info["currentStep"]
            print(f"  {shown}")
        if info["phase"] not in ("Pending", "Running"):
            break
    print(f"{run}: {info['phase']}" + (f" - {info['message']}" if info["message"] else ""))
    if info["phase"] == "Failed" and info["steps"]:
        try:
            print(get(f"/runs/{run}/logs").decode())
        except urllib.error.HTTPError:
            print("(构建 Pod 已不在,拿不到日志)")
    return 0 if info["phase"] in ("Succeeded", "Skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
