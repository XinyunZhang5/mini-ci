# 单元测试默认用 uv 临时装依赖;CI 里已经 pip install 过,用 `make test PYTHON=python3`
PYTHON ?= uv run -q --no-project --with pyyaml==6.0.2 python

.PHONY: up down demo test e2e logs

up:      ## 拉起集群和 mini-ci
	./scripts/up.sh

down:    ## 全部拆掉
	./scripts/down.sh

demo:    ## 提交并 push demo-app,触发一次流水线
	./scripts/push-demo.sh

test:    ## 单元测试(不需要集群)
	cd controller && $(PYTHON) -m unittest -v

e2e:     ## 端到端测试(需要先 make up)
	python3 tests/e2e.py

logs:    ## operator 日志
	kubectl --context kind-cicd -n ci logs deploy/ci-operator --tail=100 | grep -v healthz
