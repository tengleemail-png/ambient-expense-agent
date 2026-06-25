.PHONY: install playground run

install:
	agents-cli install

playground:
	agents-cli playground

run:
	uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8080
