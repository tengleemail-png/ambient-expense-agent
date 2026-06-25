.PHONY: install playground run generate-traces grade

install:
	agents-cli install

playground:
	agents-cli playground

run:
	uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8080

generate-traces:
	python tests/eval/generate_traces.py

grade:
	python tests/eval/grade_traces.py
