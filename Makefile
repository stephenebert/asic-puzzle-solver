PYTHON := .venv/bin/python
STAR_BATTLE_PYTHON := .venv-star-battle/bin/python
.DELETE_ON_ERROR:
# Several analysis stages read and rewrite the same generated netlists.
.NOTPARALLEL:

VENV_STAMP := .venv/.installed
STAR_BATTLE_VENV_STAMP := .venv-star-battle/.installed
PDK := vendor/sky130_fd_sc_hd
PDK_REPOSITORY := https://github.com/google/skywater-pdk-libs-sky130_fd_sc_hd.git
PDK_COMMIT := ac7fb61f06e6470b94e8afdf7c25268f62fbd7b1
WARMUP_NETLIST := build/warmup_netlist.json
PUZZLE_NETLIST := build/puzzle_netlist.json
SOLUTION := build/solution.json
STAR_BATTLE_SOLUTION := build/solution_star_battle.json
REGIONS := build/regions.json
PROTOCOL_PROOF := build/protocol_proof.json
ARCHITECTURE := build/architecture.json
EASTER_EGGS := build/easter_eggs.json
LOGO_REPORT := build/logo_report.json
LOGO_IMAGE := build/jane_street_logo.svg
KLAYOUT_NETLIST := build/puzzle_netlist_klayout.json
KLAYOUT_REPORT := build/klayout_crosscheck.json
KLAYOUT_STAMP := .venv/.klayout-installed
KLAYOUT_RESULT_STAMP := build/.klayout-crosscheck-ok
KLAYOUT_ENV ?=

.PHONY: all setup setup-star-battle pdk prepare-star-battle extract validate solve solve-z3 solve-star-battle regions test verify verify-z3 verify-star-battle compare-solvers protocol architecture easter-eggs logo klayout-results-check independent verify-independent verify-extended verify-all

all: verify

setup: $(VENV_STAMP) pdk

setup-star-battle: $(STAR_BATTLE_VENV_STAMP) pdk

$(VENV_STAMP): requirements.txt
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt
	touch $@

$(STAR_BATTLE_VENV_STAMP): requirements-star-battle.txt
	python3 -m venv .venv-star-battle
	$(STAR_BATTLE_PYTHON) -m pip install -r requirements-star-battle.txt
	touch $@

pdk:
	@mkdir -p vendor
	@if [ ! -e "$(PDK)" ]; then \
		git clone --filter=blob:none "$(PDK_REPOSITORY)" "$(PDK)"; \
	elif [ ! -d "$(PDK)/.git" ]; then \
		echo "$(PDK) exists but is not a Git repository" >&2; \
		exit 1; \
	fi
	@if ! git -C "$(PDK)" diff --quiet || ! git -C "$(PDK)" diff --cached --quiet; then \
		echo "$(PDK) has local changes. Preserve them before running setup." >&2; \
		exit 1; \
	fi
	@if ! git -C "$(PDK)" cat-file -e "$(PDK_COMMIT)^{commit}" 2>/dev/null; then \
		git -C "$(PDK)" fetch --depth 1 origin "$(PDK_COMMIT)"; \
	fi
	@if [ "$$(git -C "$(PDK)" rev-parse HEAD 2>/dev/null)" != "$(PDK_COMMIT)" ]; then \
		git -C "$(PDK)" checkout --detach "$(PDK_COMMIT)"; \
	fi
	@test "$$(git -C "$(PDK)" rev-parse HEAD)" = "$(PDK_COMMIT)"

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

solve-z3: $(PUZZLE_NETLIST) | setup
	$(PYTHON) tools/solve_symbolic.py $(PUZZLE_NETLIST) --output $(SOLUTION)

prepare-star-battle: setup-star-battle
	mkdir -p build
	$(STAR_BATTLE_PYTHON) tools/extract_netlist.py warmup/04_final.gds $(WARMUP_NETLIST)
	$(STAR_BATTLE_PYTHON) tools/extract_netlist.py puzzle.gds $(PUZZLE_NETLIST)
	$(STAR_BATTLE_PYTHON) tools/simulate_netlist.py $(WARMUP_NETLIST) --validate-warmup
	$(STAR_BATTLE_PYTHON) tools/recover_regions.py $(PUZZLE_NETLIST) --output $(REGIONS)

solve-star-battle: prepare-star-battle tools/solve_star_battle.py
	python3 -S tools/solve_star_battle.py $(PUZZLE_NETLIST) --regions $(REGIONS) --output $(STAR_BATTLE_SOLUTION)

$(REGIONS): $(PUZZLE_NETLIST) tools/recover_regions.py tools/simulate_netlist.py
	$(PYTHON) tools/recover_regions.py $(PUZZLE_NETLIST) --output $@

regions: $(REGIONS)

test: $(PUZZLE_NETLIST) $(REGIONS) tests/test_star_battle_solver.py
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

verify: validate $(SOLUTION) $(REGIONS) tools/verify_solution.py
	$(PYTHON) tools/verify_solution.py

verify-z3: verify

verify-star-battle: solve-star-battle tests/test_star_battle_solver.py
	python3 -S -m unittest discover -s tests -p 'test_star_battle_solver.py' -v
	@echo "Non-Z3 verification passed"

compare-solvers: solve-star-battle tools/solve_symbolic.py | setup
	$(PYTHON) tools/solve_symbolic.py $(PUZZLE_NETLIST) --output $(SOLUTION)
	cmp -s $(STAR_BATTLE_SOLUTION) $(SOLUTION)
	@echo "Star Battle and Z3 solution artifacts match byte-for-byte"

$(PROTOCOL_PROOF): $(PUZZLE_NETLIST) $(SOLUTION) $(REGIONS) tools/prove_protocol.py tools/solve_symbolic.py tools/verify_solution.py
	$(PYTHON) tools/prove_protocol.py $(PUZZLE_NETLIST) --solution $(SOLUTION) --regions $(REGIONS) --output $@

protocol: $(PROTOCOL_PROOF)

$(ARCHITECTURE): $(PUZZLE_NETLIST) $(REGIONS) tools/analyze_architecture.py
	$(PYTHON) tools/analyze_architecture.py --netlist $(PUZZLE_NETLIST) --regions $(REGIONS) --output $@

architecture: $(ARCHITECTURE)

$(EASTER_EGGS): $(PUZZLE_NETLIST) $(SOLUTION) $(REGIONS) example_inputs.vcd puzzle.gds tools/verify_easter_eggs.py
	$(PYTHON) tools/verify_easter_eggs.py --netlist $(PUZZLE_NETLIST) --solution $(SOLUTION) --regions $(REGIONS) --vcd example_inputs.vcd --gds puzzle.gds --output $@

easter-eggs: $(EASTER_EGGS)

logo: puzzle.gds warmup/04_final.gds tools/verify_logo.py | setup
	$(PYTHON) tools/verify_logo.py --output $(LOGO_REPORT) --svg $(LOGO_IMAGE)

$(KLAYOUT_STAMP): requirements-klayout.txt | $(VENV_STAMP)
	$(PYTHON) -m pip install -r requirements-klayout.txt
	touch $@

$(KLAYOUT_RESULT_STAMP): puzzle.gds $(PUZZLE_NETLIST) tools/extract_netlist_klayout.py $(KLAYOUT_STAMP)
	@set -e; \
	trap 'rm -f $(KLAYOUT_NETLIST) $(KLAYOUT_REPORT) $(KLAYOUT_RESULT_STAMP)' EXIT; \
	rm -f $(KLAYOUT_NETLIST) $(KLAYOUT_REPORT) $(KLAYOUT_RESULT_STAMP); \
	$(KLAYOUT_ENV) $(PYTHON) tools/extract_netlist_klayout.py puzzle.gds $(KLAYOUT_NETLIST) --compare $(PUZZLE_NETLIST) --report $(KLAYOUT_REPORT); \
	test -s $(KLAYOUT_NETLIST); \
	test -s $(KLAYOUT_REPORT); \
	touch $(KLAYOUT_RESULT_STAMP); \
	trap - EXIT

klayout-results-check: $(KLAYOUT_RESULT_STAMP)
	@if test ! -s $(KLAYOUT_NETLIST) || test ! -s $(KLAYOUT_REPORT); then \
		rm -f $(KLAYOUT_RESULT_STAMP); \
		$(MAKE) $(KLAYOUT_RESULT_STAMP); \
	fi
	@test -s $(KLAYOUT_NETLIST)
	@test -s $(KLAYOUT_REPORT)

$(KLAYOUT_NETLIST) $(KLAYOUT_REPORT): | klayout-results-check
	@test -s $@

independent: $(KLAYOUT_NETLIST) $(KLAYOUT_REPORT)

verify-independent: $(KLAYOUT_NETLIST) $(KLAYOUT_REPORT) $(SOLUTION) $(REGIONS) tools/verify_solution.py
	$(PYTHON) tools/verify_solution.py $(KLAYOUT_NETLIST) --solution $(SOLUTION) --regions $(REGIONS) --skip-extraction

verify-extended: verify verify-star-battle protocol architecture easter-eggs logo
	cmp -s $(STAR_BATTLE_SOLUTION) $(SOLUTION)
	@echo "Extended verification stages passed"

verify-all: verify-extended verify-independent
	@echo "Extended and KLayout verification stages passed"
