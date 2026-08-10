PYTHON := .venv/bin/python
.DELETE_ON_ERROR:

VENV_STAMP := .venv/.installed
PDK := vendor/sky130_fd_sc_hd
WARMUP_NETLIST := build/warmup_netlist.json
PUZZLE_NETLIST := build/puzzle_netlist.json
SOLUTION := build/solution.json
REGIONS := build/regions.json
PROTOCOL_PROOF := build/protocol_proof.json
ARCHITECTURE := build/architecture.json
EASTER_EGGS := build/easter_eggs.json
KLAYOUT_NETLIST := build/puzzle_netlist_klayout.json
KLAYOUT_REPORT := build/klayout_crosscheck.json
KLAYOUT_STAMP := .venv/.klayout-installed
KLAYOUT_RESULT_STAMP := build/.klayout-crosscheck-ok
MEDIA_STAMP := .venv/.media-installed
MEDIA_VALIDATION := build/media/validation.json
MEDIA_FULL_OUTPUTS := \
	build/media/extractor_convergence.png \
	build/media/protocol_timeline.png \
	build/media/architecture_dataflow.png \
	build/media/physical_morse.png \
	build/media/walkthrough_poster.png \
	build/media/asic_puzzle_explainer.mp4 \
	build/media/asic_puzzle_explainer.srt \
	build/media/answer_reveal.mp4 \
	build/media/answer_reveal.srt \
	build/media/qa/explainer_contact_sheet.png \
	build/media/qa/answer_reveal_contact_sheet.png \
	$(MEDIA_VALIDATION)
KLAYOUT_ENV ?=

.PHONY: all setup extract validate solve regions verify protocol architecture easter-eggs klayout-results-check independent verify-independent verify-extended media

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

$(PROTOCOL_PROOF): $(PUZZLE_NETLIST) $(SOLUTION) $(REGIONS) tools/prove_protocol.py tools/solve_symbolic.py tools/verify_solution.py
	$(PYTHON) tools/prove_protocol.py $(PUZZLE_NETLIST) --solution $(SOLUTION) --regions $(REGIONS) --output $@

protocol: $(PROTOCOL_PROOF)

$(ARCHITECTURE): $(PUZZLE_NETLIST) $(REGIONS) tools/analyze_architecture.py
	$(PYTHON) tools/analyze_architecture.py --netlist $(PUZZLE_NETLIST) --regions $(REGIONS) --output $@

architecture: $(ARCHITECTURE)

$(EASTER_EGGS): $(PUZZLE_NETLIST) $(SOLUTION) $(REGIONS) example_inputs.vcd puzzle.gds tools/verify_easter_eggs.py
	$(PYTHON) tools/verify_easter_eggs.py --netlist $(PUZZLE_NETLIST) --solution $(SOLUTION) --regions $(REGIONS) --vcd example_inputs.vcd --gds puzzle.gds --output $@

easter-eggs: $(EASTER_EGGS)

$(KLAYOUT_STAMP): requirements-klayout.txt | $(VENV_STAMP)
	$(PYTHON) -m pip install -r requirements-klayout.txt
	touch $@

$(KLAYOUT_RESULT_STAMP): puzzle.gds $(PUZZLE_NETLIST) tools/extract_netlist_klayout.py | $(KLAYOUT_STAMP)
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

verify-extended: verify protocol architecture easter-eggs verify-independent

$(MEDIA_STAMP): requirements-media.txt | $(VENV_STAMP)
	$(PYTHON) -m pip install -r requirements-media.txt
	touch $@

media: verify-independent tools/render_media.py layout.png $(SOLUTION) $(REGIONS) $(PROTOCOL_PROOF) $(ARCHITECTURE) $(EASTER_EGGS) | $(MEDIA_STAMP)
	$(PYTHON) tools/render_media.py
	@for artifact in $(MEDIA_FULL_OUTPUTS); do \
		test -s $$artifact || { echo "missing media artifact: $$artifact" >&2; exit 1; }; \
	done
