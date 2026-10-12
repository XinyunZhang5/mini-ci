// 演示模式的模拟数据:一条循环播放的流水线,加几条历史记录。
// 只在没有后端时使用(GitHub Pages,或本地加 ?demo),见 index.html。

// 界面语言跟随浏览器,?lang=en / ?lang=zh 可以强制指定
const ZH = (new URLSearchParams(location.search).get("lang") || navigator.language || "").toLowerCase().startsWith("zh");
const REPO = "XinyunZhang5/mini-ci-demo-app";

// [步骤名, 依赖, 开始秒, 结束秒, 日志]
const PLAN = [
  ["clone", [], 0, 2, ["Cloning into '/workspace/src'...", "HEAD is now at {sha} Update greeting"]],
  ["lint", ["clone"], 3, 5, ["$ python -m compileall -q .", "ok"]],
  ["test", ["clone"], 3, 7, [
    "$ python -m unittest discover -v",
    "test_greeting_mentions_mini_ci (test_app.GreetingTest.test_greeting_mentions_mini_ci) ... ok",
    "", "----------------------------------------------------------------------",
    "Ran 1 test in 0.000s", "", "OK"]],
  ["smoke", ["lint", "test"], 8, 10, ['$ python -c "import app; print(app.greeting())"', "hello from mini-ci"]],
  ["build", ["lint", "test", "smoke"], 11, 16, [
    "INFO Retrieving image manifest python:3.12-alpine",
    "INFO Built cross stage deps: map[]",
    "INFO Executing 0 build triggers",
    "INFO WORKDIR /app",
    "INFO COPY app.py .",
    "INFO Taking snapshot of files...",
    'INFO CMD ["python", "app.py"]',
    "INFO Pushing image to kind-registry:5000/mini-ci-demo-app:{sha12}",
    "INFO Pushed kind-registry:5000/mini-ci-demo-app@sha256:9f2c…"]],
  ["deploy", ["build"], 17, 21, [
    "deployment.apps/demo-app configured",
    "service/demo-app unchanged",
    'Waiting for deployment "demo-app" rollout to finish: 1 old replicas are pending termination...',
    'deployment "demo-app" successfully rolled out']],
];
const RUN_SECONDS = 21, CYCLE = 30;

const hex = (seed, n) => {
  let x = seed * 2654435761 % 4294967296, out = "";
  while (out.length < n) { x = (x * 1664525 + 1013904223) % 4294967296; out += x.toString(16).padStart(8, "0"); }
  return out.slice(0, n);
};
const iso = (ms) => new Date(ms).toISOString();
const START = Date.now();

function liveRun() {
  const now = Date.now(), cycle = Math.floor((now - START) / (CYCLE * 1000));
  const began = START + cycle * CYCLE * 1000, t = (now - began) / 1000, sha = hex(cycle + 7, 40);
  const steps = PLAN.map(([name, needs, from, to]) => {
    const state = t >= to ? "Succeeded" : t >= from ? "Running" : "Pending";
    return { name, needs, state, ...(state === "Succeeded" ? { exitCode: 0 } : {}) };
  });
  const done = steps.filter((s) => s.state === "Succeeded").length, finished = t >= RUN_SECONDS;
  const running = steps.filter((s) => s.state === "Running").map((s) => s.name);
  return {
    run: `mini-ci-demo-app-${sha.slice(0, 7)}-${hex(cycle + 99, 5)}`, repo: "mini-ci-demo-app", github: REPO,
    sha: sha.slice(0, 12), phase: finished ? "Succeeded" : "Running", message: null, steps,
    currentStep: finished ? `done (${steps.length}/${steps.length})` : `${running.join(", ") || "-"} (${done}/${steps.length})`,
    created: iso(began), finished: finished ? iso(began + RUN_SECONDS * 1000) : null,
    _t: t, _sha: sha,
  };
}

function pastRun(index, minutesAgo, failed) {
  const sha = hex(index + 1000, 40), created = START - minutesAgo * 60000;
  const steps = PLAN.map(([name, needs]) => {
    if (!failed) return { name, needs, state: "Succeeded", exitCode: 0 };
    if (name === "test") return { name, needs, state: "Failed", exitCode: 1 };
    return { name, needs, state: ["clone", "lint"].includes(name) ? "Succeeded" : "Skipped" };
  });
  return {
    run: `mini-ci-demo-app-${sha.slice(0, 7)}-${hex(index + 2000, 5)}`, repo: "mini-ci-demo-app", github: REPO,
    sha: sha.slice(0, 12), phase: failed ? "Failed" : "Succeeded", steps,
    message: failed ? (ZH ? "步骤 test 失败 (exit 1)" : "Step test failed (exit 1)") : null,
    currentStep: failed ? "test (2/6)" : "done (6/6)",
    created: iso(created), finished: iso(created + (failed ? 9 : RUN_SECONDS) * 1000),
    _t: Infinity, _sha: sha, _failed: failed,
  };
}

const HISTORY = [pastRun(1, 4, false), pastRun(2, 11, true), pastRun(3, 26, false), pastRun(4, 58, false), pastRun(5, 187, false)];
const all = () => [liveRun(), ...HISTORY];
const strip = ({ _t, _sha, _failed, ...run }) => run;

export async function runs() {
  return all().map(strip);
}

const FAILED_TEST_LOG = [
  "$ python -m unittest discover -v",
  "test_greeting_mentions_mini_ci (test_app.GreetingTest.test_greeting_mentions_mini_ci) ... FAIL",
  "", "======================================================================",
  "FAIL: test_greeting_mentions_mini_ci (test_app.GreetingTest.test_greeting_mentions_mini_ci)",
  "----------------------------------------------------------------------",
  "Traceback (most recent call last):",
  '  File "/workspace/src/test_app.py", line 8, in test_greeting_mentions_mini_ci',
  '    self.assertIn("mini-ci", greeting())',
  "AssertionError: 'mini-ci' not found in 'hello from broken build'",
  "", "----------------------------------------------------------------------",
  "Ran 1 test in 0.000s", "", "FAILED (failures=1)"];

export async function log(runName, stepName) {
  const run = all().find((r) => r.run === runName);
  const plan = PLAN.find(([name]) => name === stepName);
  if (!run || !plan) return "";
  const [, , from, to, lines] = plan;
  if (run._failed && stepName === "test") return FAILED_TEST_LOG.join("\n") + "\n";
  // 正在跑的步骤,日志按进度一行行冒出来
  const progress = Math.min(1, Math.max(0, (run._t - from) / (to - from)));
  const shown = lines.slice(0, Math.ceil(lines.length * progress));
  return shown.join("\n").replaceAll("{sha12}", run._sha.slice(0, 12)).replaceAll("{sha}", run._sha.slice(0, 7)) + (shown.length ? "\n" : "");
}
