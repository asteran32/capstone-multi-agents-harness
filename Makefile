.PHONY: configure up down test build
configure:
	python3 scripts/configure.py
up:
	docker compose build sandbox-image
	docker compose up --build -d
build:
	docker compose build
	docker compose build sandbox-image
down:
	docker compose down
test:
	docker compose run --rm --no-deps test
