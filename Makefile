PY ?= python3

.PHONY: all fixtures vocab cdm records dry load verify unload test clean demo

all: demo

fixtures:
	$(PY) scripts/make_fixtures.py

vocab:
	$(PY) -m omop_nlp_writer build-vocab

cdm:
	$(PY) -m omop_nlp_writer init-cdm

records:
	$(PY) -m omop_nlp_writer records --out build/extractions.json

dry:
	$(PY) -m omop_nlp_writer load

load:
	$(PY) -m omop_nlp_writer load --commit

verify:
	$(PY) -m omop_nlp_writer verify

unload:
	$(PY) -m omop_nlp_writer unload

test:
	$(PY) -m unittest discover -s tests -v

# Full loop from an empty checkout.
demo: clean fixtures vocab cdm records dry load verify

clean:
	rm -rf build
