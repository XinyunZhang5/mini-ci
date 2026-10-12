"""和 GitHub 打交道的两件事:读某个 commit 里的 .ci.yaml,把运行结果写回 commit status。"""

import json
import urllib.error
import urllib.request

from ciconfig import CONFIG_FILE, ConfigNotFound, FetchError

API = "https://api.github.com"
STATUS_CONTEXT = "mini-ci"


def clone_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _request(token: str, method: str, path: str, *, accept: str, body: dict | None = None) -> bytes:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mini-ci",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_config(repo: str, sha: str, token: str) -> str:
    """用 contents API 直接取文件,不用把整个仓库拉下来。"""
    try:
        raw = _request(token, "GET", f"/repos/{repo}/contents/{CONFIG_FILE}?ref={sha}",
                       accept="application/vnd.github.raw+json")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ConfigNotFound() from exc
        raise FetchError(f"GitHub 返回 {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"连不上 GitHub: {exc}") from exc
    return raw.decode()


def set_status(repo: str, sha: str, token: str, *, state: str, description: str, target_url: str) -> None:
    """state: pending / success / failure / error。同一个 context 的新状态会覆盖旧的。"""
    _request(
        token, "POST", f"/repos/{repo}/statuses/{sha}",
        accept="application/vnd.github+json",
        body={
            "state": state,
            "context": STATUS_CONTEXT,
            "description": description[:140],
            "target_url": target_url,
        },
    )
