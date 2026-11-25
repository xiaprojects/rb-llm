# rb-llm — build, run and install on a Raspberry Pi 5 (8 GB) or a dev machine.
#
#   make deps            system packages (sudo)
#   make llama           build llama.cpp (pinned tag) -> bin/
#   make model           download + verify the default model (MODEL=<id>, see `make models`)
#   make catalog         copy rbvoice case files into catalog/
#   make run             engine + API + web UI in the foreground  ->  http://localhost:8091
#   make test            unit + server tests (no model needed)
#   make check-refusal   ask GPS/position questions, report refusals (needs `make run` in another shell)
#   make install         install as systemd services in /opt/rb-llm (sudo, on the Pi)

SHELL        := /bin/bash
PYTHON       ?= python3
LLAMA_TAG    ?= v1.0.0
MODEL        ?= qwen3-1.7b
RBVOICE_DIR  ?= ../rbvoice
PORT         ?= 8091
LLAMA_PORT   ?= 8080
RB_URL       ?=
DEMO_STATUS  ?= data/demo-rb01.json
WARM_LANG    ?= it
PREFIX       ?= /opt/rb-llm

.PHONY: help deps llama llama-vulkan model models catalog engine serve run test validate \
        check-refusal bench install uninstall clean distclean

help:
	@sed -n '2,12p' Makefile | sed 's/^# \{0,1\}//'

# --- setup ------------------------------------------------------------------------------------
deps:
	sudo apt-get update
	sudo apt-get install -y build-essential cmake git curl ca-certificates python3 ccache
	@echo "optional for tests/validation on a dev machine: pip install jsonschema"

llama:
	LLAMA_TAG=$(LLAMA_TAG) ./scripts/build-llama.sh

llama-vulkan:   ## experimental: Pi 5 GPU backend (needs libvulkan-dev glslc)
	LLAMA_TAG=$(LLAMA_TAG) VULKAN=1 ./scripts/build-llama.sh

models:
	@./scripts/download-model.sh --list

model:
	./scripts/download-model.sh $(MODEL)

catalog:
	@mkdir -p catalog
	@if ls $(RBVOICE_DIR)/rbvoice-cases.*.json >/dev/null 2>&1; then \
	    cp -v $(RBVOICE_DIR)/rbvoice-cases.*.json catalog/; \
	else echo "no case files in $(RBVOICE_DIR): set RBVOICE_DIR=... or PUT /v1/catalog at runtime"; fi

# --- run --------------------------------------------------------------------------------------
engine:
	LLAMA_PORT=$(LLAMA_PORT) ./scripts/run-engine.sh

serve:
	$(PYTHON) -m rbllm serve --port $(PORT) --llama-url http://127.0.0.1:$(LLAMA_PORT) \
	    --demo-status $(DEMO_STATUS) --warm-lang $(WARM_LANG) $(if $(RB_URL),--rb-url $(RB_URL))

# engine in the background, API in the foreground; Ctrl-C stops both
run:
	@test -x bin/llama-server || { echo "run 'make llama' first"; exit 1; }
	@test -e models/current.gguf || { echo "run 'make model' first"; exit 1; }
	@test -d catalog || $(MAKE) --no-print-directory catalog
	@set -m; LLAMA_PORT=$(LLAMA_PORT) ./scripts/run-engine.sh > engine.log 2>&1 & ENGINE=$$!; \
	trap 'kill $$ENGINE 2>/dev/null; wait $$ENGINE 2>/dev/null' EXIT INT TERM; \
	echo "engine pid $$ENGINE (log: engine.log), loading model..."; \
	for i in $$(seq 1 120); do curl -sf http://127.0.0.1:$(LLAMA_PORT)/health >/dev/null && break; \
	    kill -0 $$ENGINE 2>/dev/null || { tail -20 engine.log; exit 1; }; sleep 0.5; done; \
	echo "engine ready -> web UI http://localhost:$(PORT)"; \
	$(PYTHON) -m rbllm serve --port $(PORT) --llama-url http://127.0.0.1:$(LLAMA_PORT) \
	    --demo-status $(DEMO_STATUS) --warm-lang $(WARM_LANG) $(if $(RB_URL),--rb-url $(RB_URL))

# --- quality ----------------------------------------------------------------------------------
test:
	$(PYTHON) -m unittest discover -s tests -v

validate:
	$(PYTHON) tools/validate_examples.py

check-refusal:
	$(PYTHON) scripts/refusal_check.py --url http://127.0.0.1:$(PORT)

bench:
	./bin/llama-bench -m models/current.gguf -t $$(nproc) -p 64,512 -n 32 -r 3 | tee bench-$$(cat models/current.id).txt

# --- install on the Pi ------------------------------------------------------------------------
install:
	@test -x bin/llama-server && test -e models/current.gguf || { echo "build and download first: make llama model"; exit 1; }
	sudo useradd --system --home $(PREFIX) --shell /usr/sbin/nologin rbllm 2>/dev/null || true
	sudo mkdir -p $(PREFIX)/models
	sudo cp -r bin rbllm scripts web schemas data $(PREFIX)/
	sudo cp -r catalog $(PREFIX)/ 2>/dev/null || true
	sudo cp models/registry.tsv models/current.id $(PREFIX)/models/
	sudo cp -L models/current.gguf $(PREFIX)/models/$$(readlink models/current.gguf)
	sudo ln -sfn $$(readlink models/current.gguf) $(PREFIX)/models/current.gguf
	test -f /etc/default/rb-llm || sudo cp systemd/rb-llm.default /etc/default/rb-llm
	sudo cp systemd/rb-llm-engine.service systemd/rb-llm.service /etc/systemd/system/
	sudo chown -R rbllm:rbllm $(PREFIX)
	sudo systemctl daemon-reload
	sudo systemctl enable --now rb-llm-engine rb-llm
	@echo "installed: http://$$(hostname -I | awk '{print $$1}'):$(PORT)  (edit /etc/default/rb-llm for RB-01 URL)"

uninstall:
	sudo systemctl disable --now rb-llm rb-llm-engine 2>/dev/null || true
	sudo rm -f /etc/systemd/system/rb-llm.service /etc/systemd/system/rb-llm-engine.service
	sudo systemctl daemon-reload
	@echo "left in place: $(PREFIX) and /etc/default/rb-llm (remove by hand)"

clean:
	rm -rf third_party/llama.cpp/build engine.log bench-*.txt
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

distclean: clean
	rm -rf third_party bin
	@echo "models/ kept (large downloads): rm models/*.gguf to remove"
