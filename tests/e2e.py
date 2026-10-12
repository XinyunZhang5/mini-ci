#!/usr/bin/env python3
"""端到端测试:对着一个真的集群(./scripts/up.sh 拉起的那个)把主要场景跑一遍。

每个用例往集群内的 git 服务推一个临时分支,发一个签名的 webhook,然后检查
PipelineRun 的最终状态和集群里实际发生的事。用的仓库名是 e2e-app,
和 demo-app 的运行记录、部署互不干扰。

用法: python3 tests/e2e.py [-v] [用例名...]
"""

import hashlib
import hmac
import json
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

WEBHOOK = "http://localhost:18080"
GIT_REMOTE = "git://localhost:19418/demo-app.git"
CLONE_URL = "git://git-server.ci.svc.cluster.local:9418/demo-app.git"  # 集群内看到的地址
REPO = "e2e-app"
CONTEXT = "kind-cicd"
TIMEOUT = 300

STEP = "{{name: {name}, image: 'alpine:3.20', run: '{run}'{needs}}}"


def step(name: str, run: str, needs: list[str] | None = None) -> str:
    return "  - " + STEP.format(name=name, run=run, needs=f", needs: [{', '.join(needs)}]" if needs else "")


def kubectl(*args: str, namespace: str | None = "ci", check: bool = True) -> str:
    cmd = ["kubectl", "--context", CONTEXT, *(["-n", namespace] if namespace else []), *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True).stdout


def kubectl_json(*args: str, **kwargs) -> dict:
    return json.loads(kubectl(*args, "-o", "json", **kwargs))


def webhook_secret() -> bytes:
    return kubectl("get", "secret", "webhook-secret", "-o", "go-template={{.data.secret | base64decode}}").strip().encode()


def push(files: dict[str, str]) -> str:
    """把这些文件作为一个新 commit 推到一个临时分支,返回 commit sha。"""
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-c", "user.name=e2e", "-c", "user.email=e2e@localhost", *args],
                cwd=tmp, check=True, capture_output=True, text=True,
            ).stdout.strip()

        git("init", "-q", "-b", "main")
        for path, content in files.items():
            target = Path(tmp, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git("add", "-A")
        git("commit", "-q", "--allow-empty", "-m", "e2e")
        git("push", "-q", GIT_REMOTE, f"HEAD:refs/heads/e2e/{uuid.uuid4().hex[:10]}")
        return git("rev-parse", "HEAD")


def post_webhook(sha: str, *, signature: str | None = None) -> dict:
    body = json.dumps({"ref": "refs/heads/e2e", "after": sha, "repository": {"name": REPO, "clone_url": CLONE_URL}}).encode()
    if signature is None:
        signature = "sha256=" + hmac.new(webhook_secret(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        WEBHOOK + "/webhook", data=body,
        headers={"Content-Type": "application/json", "X-GitHub-Event": "push", "X-Hub-Signature-256": signature},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def get_run(name: str) -> dict:
    with urllib.request.urlopen(f"{WEBHOOK}/runs/{name}", timeout=30) as resp:
        return json.load(resp)


def wait_for(what: str, condition, timeout: int = TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(1)
    raise AssertionError(f"等了 {timeout}s 仍未满足: {what}")


def start(config: str | None, extra_files: dict[str, str] | None = None) -> str:
    files = dict(extra_files or {})
    if config is not None:
        files[".ci.yaml"] = config
    return post_webhook(push(files))["run"]


def finish(name: str) -> dict:
    return wait_for(f"{name} 结束", lambda: (r := get_run(name))["phase"] not in ("Pending", "Running") and r)


def states(run: dict) -> dict[str, str]:
    return {s["name"]: s["state"] for s in run["steps"]}


def step_times(run_name: str) -> dict[str, tuple[str, str]]:
    """步骤名 -> (容器开始时间, 结束时间)"""
    pods = kubectl_json("get", "pods", "-l", f"ci/run={run_name}")["items"]
    times = {}
    for pod in pods:
        terminated = pod["status"]["containerStatuses"][0]["state"]["terminated"]
        times[pod["metadata"]["labels"]["ci/step"]] = (terminated["startedAt"], terminated["finishedAt"])
    return times


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            urllib.request.urlopen(WEBHOOK + "/healthz", timeout=5)
        except OSError as exc:
            raise unittest.SkipTest(f"连不上 {WEBHOOK},先跑 ./scripts/up.sh ({exc})")

    @classmethod
    def tearDownClass(cls):
        kubectl("delete", "pipelineruns", "-l", f"ci/repo={REPO}", "--wait=false", check=False)
        kubectl("delete", "deployment,service", REPO, "--ignore-not-found", namespace="apps", check=False)

    def test_webhook_rejects_bad_signature(self):
        sha = "0" * 39 + "1"
        for signature in ("sha256=" + "0" * 64, ""):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post_webhook(sha, signature=signature)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

    def test_independent_steps_run_in_parallel_and_dependents_wait(self):
        name = start("steps:\n" + "\n".join([
            step("a", "sleep 4"),
            step("b", "sleep 4"),
            step("c", "echo done", needs=["a", "b"]),
        ]))
        run = finish(name)
        self.assertEqual(run["phase"], "Succeeded", run)
        self.assertEqual(states(run), {"clone": "Succeeded", "a": "Succeeded", "b": "Succeeded", "c": "Succeeded"})

        t = step_times(name)
        self.assertLess(t["a"][0], t["b"][1], "a 和 b 应该有重叠的运行时间")
        self.assertLess(t["b"][0], t["a"][1], "a 和 b 应该有重叠的运行时间")
        self.assertGreaterEqual(t["a"][0], t["clone"][1], "a 应在 clone 之后")
        self.assertGreaterEqual(t["c"][0], max(t["a"][1], t["b"][1]), "c 应在 a、b 都结束之后")

    def test_failed_step_skips_dependents_but_siblings_finish(self):
        name = start("steps:\n" + "\n".join([
            step("bad", "echo boom; exit 3"),
            step("slow", "sleep 5"),
            step("after", "echo never", needs=["bad", "slow"]),
        ]))
        run = finish(name)
        self.assertEqual(run["phase"], "Failed")
        self.assertEqual(states(run), {"clone": "Succeeded", "bad": "Failed", "slow": "Succeeded", "after": "Skipped"})
        self.assertIn("bad", run["message"])
        self.assertIn("exit 3", run["message"])
        with urllib.request.urlopen(f"{WEBHOOK}/runs/{name}/logs/bad", timeout=30) as resp:
            self.assertIn("boom", resp.read().decode())

    def test_invalid_config_fails_before_anything_runs(self):
        name = start("steps:\n" + "\n".join([step("a", "true", needs=["b"]), step("b", "true", needs=["a"])]))
        run = finish(name)
        self.assertEqual(run["phase"], "Failed")
        self.assertIn("循环", run["message"])
        self.assertEqual(kubectl("get", "pods", "-l", f"ci/run={name}", "-o", "name").strip(), "")

    def test_commit_without_config_is_skipped(self):
        run = finish(start(None, {"README": "no ci config here\n"}))
        self.assertEqual(run["phase"], "Skipped")

    def test_deleting_a_run_cancels_it(self):
        name = start("steps:\n" + step("slow", "sleep 600"))
        wait_for("slow 开始运行", lambda: states(get_run(name)).get("slow") == "Running"
                 and kubectl("get", "pod", f"{name}-slow", "-o", "jsonpath={.status.phase}", check=False) == "Running")
        kubectl("delete", "pipelinerun", name)
        wait_for("Pod 和工作区被回收", lambda: not kubectl(
            "get", "pods,pvc", "-l", f"ci/run={name}", "-o", "name", check=False).strip(), timeout=120)

    def test_run_survives_operator_restart(self):
        name = start("steps:\n" + "\n".join([step("first", "sleep 10"), step("second", "echo ok", needs=["first"])]))
        wait_for("first 开始运行", lambda: states(get_run(name)).get("first") == "Running")
        # first 结束的那一刻 operator 不在;second 得靠重启后的 operator 发现并启动
        kubectl("delete", "pod", "-l", "app=ci-operator", "--wait=false")
        run = finish(name)
        self.assertEqual(run["phase"], "Succeeded", run)
        pods = kubectl("get", "pods", "-l", f"ci/run={name},ci/step=second", "-o", "name").split()
        self.assertEqual(len(pods), 1, "second 应该恰好被启动一次")

    def test_build_and_deploy_and_steps_have_no_cluster_credentials(self):
        deploy = f"""\
apiVersion: apps/v1
kind: Deployment
metadata: {{name: {REPO}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: {REPO}}}}}
  template:
    metadata: {{labels: {{app: {REPO}}}}}
    spec:
      containers:
        - {{name: app, image: __IMAGE__, command: [sleep, infinity]}}
"""
        token = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        sha = push({
            ".ci.yaml": "steps:\n" + step("check", f"test ! -e {token}") + "\nbuild: {}\ndeploy: {}\n",
            "Dockerfile": "FROM alpine:3.20\nCOPY marker /marker\n",
            "marker": uuid.uuid4().hex,
            "deploy.yaml": deploy,
        })
        name = post_webhook(sha)["run"]
        run = finish(name)
        self.assertEqual(run["phase"], "Succeeded", run)
        self.assertEqual(list(states(run)), ["clone", "check", "build", "deploy"])

        deployed = kubectl_json("get", "deployment", REPO, namespace="apps")
        self.assertTrue(deployed["spec"]["template"]["spec"]["containers"][0]["image"].endswith(f"/{REPO}:{sha[:12]}"))
        self.assertEqual(deployed["status"].get("availableReplicas"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
