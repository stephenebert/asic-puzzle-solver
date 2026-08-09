PYTHON := .venv/bin/python
VENV_STAMP := .venv/.installed
PDK := vendor/sky130_fd_sc_hd
WARMUP_NETLIST := build/warmup_netlist.json
PUZZLE_NETLIST := build/puzzle_netlist.json
SOLUTION := build/solution.json
REGIONS := build/regions.json

.PHONY: all setup extract validate solve regions verify

all: verify

setup: $(VENV_STAMP) $(PDK)

$(VENV_STAMP): requirements.txt
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt
	touch $@

$(PDK):
	mkdir -p vendor
	git clone --depth 1 https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd.git $(PDK)

$(WARMUP_NETLIST): warmup/04_final.gds tools/extract_netlist.py | setup
	mkdir -p build
	$(PYTHON) tools/extract_netlist.py warmup/04_final.gds $@

$(PUZZLE_NETLIST): puzzle.gds tools/extract_netlist.py | setup
	mkdir -p build
	$(PYTHON) tools/extract_netlist.py puzzle.gds $@

extract: $(WARMUP_NETLIST) $(PUZZLE_NETLIST)

validate: $(WARMUP_NETLIST)
	$(PYTHON) tools/simulate_netlist.py $< --validate-warmup

$(SOLUTION): $(PUZZLE_NETLIST) tools/solve_symbolic.py tools/simulate_netlist.py
	$(PYTHON) tools/solve_symbolic.py $(PUZZLE_NETLIST) --output $@

solve: $(SOLUTION)

$(REGIONS): $(PUZZLE_NETLIST) tools/recover_regions.py tools/simulate_netlist.py
	$(PYTHON) tools/recover_regions.py $(PUZZLE_NETLIST) --output $@

regions: $(REGIONS)

verify: validate $(SOLUTION) $(REGIONS) tools/verify_solution.py
	$(PYTHON) tools/verify_solution.py
