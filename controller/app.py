"""mini-ci webhook:接收 GitHub 格式的 push webhook,为每次 push 创建一个 PipelineRun。

它只负责"记下有一次运行要跑",真正去跑的是 reconciler.py 里的 operator。
"""

import asyncio
import hashlib
import hmac
import json
import os
import re

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from kubernetes import client, config

import github
from pipeline import CI_NAMESPACE, GROUP, PLURAL, VERSION, pod_name

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].encode()

REPO_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
INDEX_HTML = os.path.join(os.path.dirname(__file__), "static", "index.html")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ZERO_SHA = "0" * 40

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()  # 在集群外本地调试时用 ~/.kube/config
custom = client.CustomObjectsApi()
core = client.CoreV1Api()

app = FastAPI(title="mini-ci")


def _verify_signature(body: bytes, signature: str | None) -> None:
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "bad signature")


def _summary(run: dict) -> dict:
    status = run.get("status") or {}
    return {
        "run": run["metadata"]["name"],
        "repo": run["spec"]["repo"],
        "sha": run["spec"]["sha"][:12],
        "github": (run["spec"].get("github") or {}).get("repo"),
        "phase": status.get("phase", "Pending"),
        "currentStep": status.get("currentStep"),
        "message": status.get("message"),
        "steps": status.get("steps", []),
        "created": run["metadata"]["creationTimestamp"],
        "finished": status.get("finishedAt"),
    }


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_HTML)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
):
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)

    if x_github_event == "ping":
        return {"msg": "pong"}
    if x_github_event != "push":
        return {"msg": f"ignored event: {x_github_event}"}

    payload = json.loads(body)
    sha = payload.get("after", "")
    if sha == ZERO_SHA:
        return {"msg": "ignored: branch deleted"}
    repository = payload.get("repository", {})
    repo = repository.get("name", "").lower()
    clone_url = repository.get("clone_url", "")
    if not REPO_RE.match(repo) or not SHA_RE.match(sha) or not clone_url:
        raise HTTPException(400, "invalid repository name / sha / clone_url")

    run = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "PipelineRun",
        "metadata": {
            "generateName": f"{repo}-{sha[:7]}-",
            "labels": {"ci/repo": repo, "ci/sha": sha[:12]},
        },
        "spec": {"repo": repo, "cloneUrl": clone_url, "sha": sha},
    }
    # 确实是 GitHub 上的仓库才打上 github 标记(operator 据此回写 commit status、带 token 去 clone)
    full_name = repository.get("full_name", "")
    if GITHUB_REPO_RE.match(full_name) and clone_url == github.clone_url(full_name):
        run["spec"]["github"] = {"repo": full_name}
    created = await asyncio.to_thread(
        custom.create_namespaced_custom_object, GROUP, VERSION, CI_NAMESPACE, PLURAL, run
    )
    return {"run": created["metadata"]["name"], "repo": repo, "sha": sha}


@app.get("/runs")
def list_runs():
    runs = custom.list_namespaced_custom_object(GROUP, VERSION, CI_NAMESPACE, PLURAL)["items"]
    runs.sort(key=lambda r: r["metadata"]["creationTimestamp"], reverse=True)
    return [_summary(r) for r in runs]


@app.get("/runs/{name}")
def get_run(name: str):
    try:
        return _summary(custom.get_namespaced_custom_object(GROUP, VERSION, CI_NAMESPACE, PLURAL, name))
    except client.ApiException as exc:
        if exc.status == 404:
            raise HTTPException(404, "run not found")
        raise


@app.get("/runs/{name}/logs/{step}", response_class=PlainTextResponse)
def step_logs(name: str, step: str):
    if step not in {s["name"] for s in get_run(name)["steps"]}:
        raise HTTPException(404, "step not found")
    try:
        return core.read_namespaced_pod_log(pod_name(name, step), CI_NAMESPACE)
    except client.ApiException:
        return ""


@app.get("/runs/{name}/logs", response_class=PlainTextResponse)
def run_logs(name: str):
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise HTTPException(404, "run not found")
    steps = get_run(name)["steps"]
    if not steps:
        raise HTTPException(404, "这次运行没有可跑的步骤")
    out = []
    for step in steps:
        out.append(f"===== {step['name']} [{step['state']}] =====")
        if step["state"] in ("Pending", "Skipped"):
            continue
        try:
            out.append(core.read_namespaced_pod_log(pod_name(name, step["name"]), CI_NAMESPACE))
        except client.ApiException:
            out.append("(还没有日志)\n")
    return "\n".join(out)
