# =============================================================================
# AI-Driven SDN Traffic Optimization and Congestion Avoidance
# =============================================================================
SHELL      := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

VENV       ?= .venv
PY         := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
SUDO_ENV   := sudo env "PATH=$(CURDIR)/$(VENV)/bin:$$PATH"
MN         := $(SUDO_ENV) $(VENV)/bin/mn
MN_        := $(VENV)/bin/mn
RYU        := $(VENV)/bin/ryu-manager
CONFIG     ?= config.yaml

.DEFAULT_GOAL := help
.PHONY: help setup setup-native verify test clean \
        topo topo-graph verify-p2 controller verify-p3 portmap \
        topo-discovery verify-p4 verify-p4-live \
        api api-sudo dashboard dashboard-install verify-p12 \
        snapshot snapshot-all snapshot-list \
        dataset train train-quick verify-p7 \
        experiments experiments-dry experiments-resume verify-p10 \
        figures demo \
        ping check-native mn-clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

# -----------------------------------------------------------------------------
# P1 -- environment
# -----------------------------------------------------------------------------
setup: ## Create the Python 3.9 venv and install pinned requirements
	@test -x $(PY) || { echo "ERROR: $(VENV) missing. Install CPython 3.9 first (see README)."; exit 1; }
	$(PIP) install --upgrade 'pip<26' wheel
	$(PIP) install -r requirements.txt
	VENV=$(VENV) bash scripts/install_ryu.sh
	VENV=$(VENV) bash scripts/install_mininet.sh
	@echo "==> setup complete. Run 'make verify' for the P1 acceptance test."

setup-native: ## Install native deps (needs root): openvswitch, iperf3, mininet
	sudo modprobe openvswitch
	sudo systemctl enable --now ovsdb-server ovs-vswitchd
	sudo pacman -S --needed openvswitch iperf3 net-tools ethtool psmisc socat
	@echo "==> Mininet is not packaged for this distro; see README for the git install"

check-native: ## Verify the native SDN stack is healthy
	@echo "== openvswitch =="; ovs-vsctl --version | head -1
	@echo "== kernel datapath module =="; \
	  if lsmod | grep '^openvswitch' >/dev/null; then \
	    echo "   openvswitch: LOADED"; \
	  else \
	    echo "   openvswitch: NOT LOADED (netdev fallback required)"; \
	  fi
	@echo "== services =="; systemctl is-active ovsdb-server ovs-vswitchd
	@echo "== mininet =="; $(MN_) --version
	@echo "== ryu =="; $(RYU) --version
	@echo "== iperf3 =="; iperf3 --version | head -1

verify: ## P1 acceptance: remote controller + full-mesh ping, 0% loss
	@bash scripts/verify_p1.sh

test: ## Run the unit test suite
	$(PY) -m pytest tests/ -v

ping: ## Convenience: run the test suite quietly
	$(PY) -m pytest tests/ -q

clean: ## Remove generated artefacts (keeps venv and source)
	rm -rf data/telemetry.csv data/logs results/figures
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '.pytest_cache' -type d -prune -exec rm -rf {} +
	@echo "==> cleaned"

mn-clean: ## Tear down any leftover mininet state (needs root)
	-$(MN) -c

# -----------------------------------------------------------------------------
# P12 -- API + dashboard
# -----------------------------------------------------------------------------
api: ## P12 -- run the FastAPI (add sudo to read OpenFlow tables)
	$(PY) -m uvicorn future.api.main:app --host 127.0.0.1 --port 8000

api-sudo: ## P12 -- run the API with OpenFlow read access
	sudo $(PY) -m uvicorn future.api.main:app --host 127.0.0.1 --port 8000

dashboard: ## P12 -- run the React dev server against the API
	cd future/dashboard/react-app && npm run dev

dashboard-install: ## P12 -- install dashboard dependencies
	cd future/dashboard/react-app && npm install

verify-p12: ## P12 acceptance: auth, endpoints, websocket, dashboard build
	bash scripts/verify_p12.sh

# -----------------------------------------------------------------------------
# Progress snapshots (evidence, not intent)
# -----------------------------------------------------------------------------
snapshot: ## Capture a verified progress snapshot (no root targets)
	$(PY) scripts/snapshot.py

snapshot-all: ## Capture every target including root-only ones
	$(PY) scripts/snapshot.py --all

snapshot-list: ## List snapshot targets
	$(PY) scripts/snapshot.py --list

# -----------------------------------------------------------------------------
# Later phases -- declared now so the interface is stable, implemented per phase.
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# P2 -- multipath topology
# -----------------------------------------------------------------------------
topo: ## P2 -- build the diamond topology and block (needs root)
	$(SUDO_ENV) $(PY) -m topology.multipath_topology --topo-only

topo-graph: ## P2 -- print the topology + all h1->h4 paths (no root needed)
	$(PY) -m topology.multipath_topology --paths h1 h4

verify-p2: ## P2 acceptance: both paths in isolation (needs root)
	$(SUDO_ENV) $(PY) scripts/verify_p2.py --out results/p2_topology.json

# -----------------------------------------------------------------------------
# P3 -- loop-safe controller
# -----------------------------------------------------------------------------
controller: ## P3 -- run ryu-manager with our loop-safe controller (needs data/portmap.json)
	PYTHONPATH=. $(RYU) --ofp-listen-host 127.0.0.1 --ofp-tcp-listen-port 6653 \
		controller.sdn_controller

verify-p3: ## P3 acceptance: pingall, no storm, flows, throughput, restart (needs root)
	$(SUDO_ENV) $(PY) scripts/verify_p3.py --out results/p3_controller.json

portmap: ## P3 -- dump switch/host port adjacency to data/portmap.json (needs root)
	$(SUDO_ENV) $(PY) -m topology.multipath_topology --dump-portmap data/portmap.json
# -----------------------------------------------------------------------------
# P4 -- topology discovery
# -----------------------------------------------------------------------------
topo-discovery: ## P4 -- print discovered NetworkX graph + paths (no root needed)
	$(PY) -m controller.topology --paths h1 h4

verify-p4: ## P4 acceptance: NetworkX graph, capacities, k-shortest paths, LLDP
	$(PY) scripts/verify_p4.py --out results/p4_topology.json

verify-p4-live: ## P4 acceptance (live, needs root): Mininet + Ryu real LLDP discovery & link failure
	$(SUDO_ENV) $(PY) scripts/verify_p4_live.py --out results/p4_live.json

# -----------------------------------------------------------------------------
# P5 -- telemetry & active probes
# -----------------------------------------------------------------------------
verify-p5: ## P5 acceptance: rate calculation, wrap/reset, active probes, CSV (no root needed)
	$(PY) scripts/verify_p5.py --out results/p5_telemetry.json

verify-p5-live: ## P5 acceptance (live, needs root): Mininet + Ryu + iperf3 rates & latency under load
	$(SUDO_ENV) $(PY) scripts/verify_p5_live.py --out results/p5_live.json

# -----------------------------------------------------------------------------
# P6 -- dataset generation
# -----------------------------------------------------------------------------
dataset: verify-p6 ## P6 -- generate the labelled telemetry dataset
dataset-smoke: ## P6 quick smoke collection (needs root): 1 run per pattern, 30s, writes to data/smoke/
	$(SUDO_ENV) $(PY) ml/collect_dataset.py --smoke

dataset-live: ## P6 full live data collection (needs root): 10 runs per pattern, 90s, writes to data/
	$(SUDO_ENV) $(PY) ml/collect_dataset.py --live --runs-per-pattern 10 --duration 90

check-dataset: ## P6 quality check: validate live dataset integrity
	$(PY) scripts/check_live_dataset.py --csv data/telemetry_live_labeled.csv

check-smoke: ## P6 quality check: validate smoke dataset integrity
	$(PY) scripts/check_live_dataset.py --csv data/smoke/telemetry_live_labeled.csv

verify-p6: ## P6 acceptance: traffic patterns, future labeling, balanced dataset
	$(PY) scripts/verify_p6.py --out results/p6_dataset.json
train:       ## P7 -- train + evaluate the congestion predictor
	$(PY) -m ml.train

train-quick: ## P7 -- fast sanity training (fewer trees)
	$(PY) -m ml.train --quick

verify-p7: ## P7 acceptance: features, leakage guards, model, bundle
	$(PY) scripts/verify_p7.py --out results/p7_acceptance.json
experiments: ## P10 -- 4 modes x scenarios x repetitions (needs root, long)
	$(PY) scripts/run_experiments.py

experiments-dry: ## P10 -- print the run plan without touching mininet
	$(PY) scripts/run_experiments.py --dry-run

experiments-resume: ## P10 -- continue an interrupted sweep, retrying failures
	$(PY) scripts/run_experiments.py --resume

verify-p10: ## P10 acceptance: experiment CSV schema, mode coverage, no silent gaps
	$(PY) scripts/verify_p10.py --out results/p10_acceptance.json
figures:     ## P11 -- comparison tables, box plots, timeline plots
	@echo "P11 (analysis): not implemented yet -- say 'start P11'"
demo:        ## P13 -- end-to-end demonstration
	@echo "P13 (demo): not implemented yet -- say 'start P13'"