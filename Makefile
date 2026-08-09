PYTHON := .venv/bin/python
PDK := vendor/sky130_fd_sc_hd

.PHONY: all setup extract validate solve regions

all: extract validate solve regions

setup: $(PYTHON) $(PDK)

$(PYTHON): requirements.txt
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt

$(PDK):
	mkdir -p vendor
	git clone --depth 1 https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd.git $(PDK)

extract: setup
	$(PYTHON) tools/extract_netlist.py warmup/04_final.gds build/warmup_netlist.json
	$(PYTHON) tools/extract_netlist.py puzzle.gds build/puzzle_netlist.json

validate: extract
	$(PYTHON) tools/simulate_netlist.py build/warmup_netlist.json --validate-warmup

solve: extract
	$(PYTHON) tools/solve_symbolic.py build/puzzle_netlist.json

regions: extract
	$(PYTHON) tools/recover_regions.py build/puzzle_netlist.json
