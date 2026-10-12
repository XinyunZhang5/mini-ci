import unittest

from ciconfig import ConfigError, from_dict, parse, to_dict

VALID = """
steps:
  - name: test
    image: python:3.12-alpine
    run: python -m unittest
    env: {RETRIES: 3}
build: {}
deploy:
  rollout: deployment/web
"""


class ParseTest(unittest.TestCase):
    def test_valid_config_with_defaults(self):
        p = parse(VALID)
        self.assertEqual([s.name for s in p.steps], ["test"])
        self.assertEqual(p.steps[0].env, {"RETRIES": "3"})
        self.assertEqual(p.build.dockerfile, "Dockerfile")
        self.assertEqual(p.deploy.manifest, "deploy.yaml")
        self.assertEqual(p.deploy.rollout, "deployment/web")

    def test_build_and_deploy_are_optional(self):
        p = parse("steps: [{name: t, image: alpine, run: 'true'}]")
        self.assertIsNone(p.build)
        self.assertIsNone(p.deploy)

    def test_needs_are_parsed_and_steps_sorted_by_dependency(self):
        p = parse("""
steps:
  - {name: c, image: alpine, run: x, needs: [a, b]}
  - {name: a, image: alpine, run: x}
  - {name: b, image: alpine, run: x, needs: [a]}
""")
        self.assertEqual([s.name for s in p.steps], ["a", "b", "c"])
        self.assertEqual(p.steps[2].needs, ["a", "b"])

    def test_round_trips_through_dict(self):
        for text in (VALID, "steps: [{name: t, image: alpine, run: 'true'}]"):
            p = parse(text)
            self.assertEqual(from_dict(to_dict(p)), p)

    def assertRejected(self, text, fragment):
        with self.assertRaises(ConfigError) as ctx:
            parse(text)
        self.assertIn(fragment, str(ctx.exception))

    def test_rejects_bad_configs(self):
        step = "{name: t, image: alpine, run: 'true'}"
        cases = [
            ("steps: [", "YAML"),
            ("- a", "映射"),
            ("steps: []", "非空列表"),
            ("build: {}", "非空列表"),
            ("steps: [{name: t, image: alpine}]", "run"),
            ("steps: [{name: Bad_Name, image: alpine, run: x}]", "小写"),
            ("steps: [{name: deploy, image: alpine, run: x}]", "保留名"),
            (f"steps: [{step}, {step}]", "重复"),
            (f"steps: [{step}]\nextra: 1", "不认识"),
            ("steps: [{name: t, image: alpine, run: x, needs: t}]", "列表"),
            ("steps: [{name: t, image: alpine, run: x, needs: [t]}]", "依赖自己"),
            ("steps: [{name: t, image: alpine, run: x, needs: [nope]}]", "不存在"),
            ("steps: [{name: a, image: i, run: x, needs: [b]}, {name: b, image: i, run: x, needs: [a]}]", "循环"),
            ("steps: [{name: t, image: alpine, run: x, env: {'1A': x}}]", "变量名"),
            (f"steps: [{step}]\nbuild: {{dockerfile: ../../etc/passwd}}", "相对路径"),
            (f"steps: [{step}]\ndeploy: {{manifest: /etc/x}}", "相对路径"),
            (f"steps: [{step}]\ndeploy: {{rollout: 'pod/x; rm -rf'}}", "rollout"),
        ]
        for text, fragment in cases:
            with self.subTest(text=text):
                self.assertRejected(text, fragment)


if __name__ == "__main__":
    unittest.main()
