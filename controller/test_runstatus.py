import unittest

from runstatus import observe, plan

#        clone
#        /   \
#     lint   test
#        \   /
#        build
GRAPH = [("clone", []), ("lint", ["clone"]), ("test", ["clone"]), ("build", ["lint", "test"])]


def pod(phase, exit_code=None, reason=None):
    status = {"phase": phase}
    if exit_code is not None:
        status["containerStatuses"] = [{"state": {"terminated": {"exitCode": exit_code}}}]
    if reason:
        status["reason"] = reason
    return {"status": status}


OK = pod("Succeeded", 0)


def states(result):
    return {s["name"]: s["state"] for s in result["steps"]}


class ObserveTest(unittest.TestCase):
    def test_pod_phases(self):
        self.assertEqual(observe(pod("Pending"))["state"], "Running")
        self.assertEqual(observe(pod("Running"))["state"], "Running")
        self.assertEqual(observe(OK), {"state": "Succeeded", "exitCode": 0})
        self.assertEqual(observe(pod("Failed", 2)), {"state": "Failed", "exitCode": 2})
        self.assertEqual(observe(pod("Failed", reason="DeadlineExceeded"))["reason"], "DeadlineExceeded")


class PlanTest(unittest.TestCase):
    def test_fresh_run_starts_only_the_root(self):
        r = plan(GRAPH, {}, {})
        self.assertEqual(r["start"], ["clone"])
        self.assertEqual(states(r), {"clone": "Running", "lint": "Pending", "test": "Pending", "build": "Pending"})
        self.assertEqual(r["phase"], "Running")

    def test_independent_steps_start_together(self):
        r = plan(GRAPH, {"clone": OK}, {})
        self.assertEqual(r["start"], ["lint", "test"])
        self.assertEqual(r["currentStep"], "lint, test (1/4)")

    def test_running_steps_are_not_started_again(self):
        r = plan(GRAPH, {"clone": OK, "lint": pod("Running"), "test": pod("Pending")}, {})
        self.assertEqual(r["start"], [])

    def test_step_waits_for_all_of_its_needs(self):
        r = plan(GRAPH, {"clone": OK, "lint": OK, "test": pod("Running")}, {})
        self.assertEqual(r["start"], [])
        self.assertEqual(states(r)["build"], "Pending")

    def test_step_starts_once_all_needs_succeeded(self):
        r = plan(GRAPH, {"clone": OK, "lint": OK, "test": OK}, {})
        self.assertEqual(r["start"], ["build"])

    def test_all_done(self):
        r = plan(GRAPH, {n: OK for n, _ in GRAPH}, {})
        self.assertEqual(r["phase"], "Succeeded")
        self.assertEqual(r["currentStep"], "done (4/4)")
        self.assertNotIn("message", r)

    def test_failure_skips_dependents_but_lets_siblings_finish(self):
        r = plan(GRAPH, {"clone": OK, "lint": pod("Failed", 1), "test": pod("Running")}, {})
        self.assertEqual(states(r), {"clone": "Succeeded", "lint": "Failed", "test": "Running", "build": "Skipped"})
        self.assertEqual(r["phase"], "Running")  # test 还在跑
        self.assertEqual(r["start"], [])

        r = plan(GRAPH, {"clone": OK, "lint": pod("Failed", 1), "test": OK}, {})
        self.assertEqual(r["phase"], "Failed")
        self.assertEqual(r["message"], "步骤 lint 失败 (exit 1)")

    def test_skip_propagates_down_the_chain(self):
        chain = [("a", []), ("b", ["a"]), ("c", ["b"])]
        r = plan(chain, {"a": pod("Failed", 3)}, {})
        self.assertEqual(states(r), {"a": "Failed", "b": "Skipped", "c": "Skipped"})
        self.assertEqual(r["phase"], "Failed")

    def test_timeout_message(self):
        r = plan([("a", [])], {"a": pod("Failed", reason="DeadlineExceeded")}, {})
        self.assertEqual(r["message"], "步骤 a 超时")

    def test_finished_step_whose_pod_is_gone_is_not_rerun(self):
        previous = {"clone": {"name": "clone", "needs": [], "state": "Succeeded", "exitCode": 0}}
        r = plan(GRAPH, {}, previous)
        self.assertEqual(states(r)["clone"], "Succeeded")
        self.assertEqual(r["start"], ["lint", "test"])


if __name__ == "__main__":
    unittest.main()
