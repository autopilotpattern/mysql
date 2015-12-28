MAKEFLAGS += --warn-undefined-variables
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail
.DEFAULT_GOAL := build

build:
	docker build -t="0x74696d/triton-mysql" .

ship:
	docker push 0x74696d/triton-mysql

# run a 3-data-node cluster on Triton
run:
	docker-compose -p my up -d
	docker-compose -p my scale mysql=3

# for testing against Docker locally
test:
	docker-compose -p my -f local-compose.yml stop || true
	docker-compose -p my -f local-compose.yml rm -f || true
	docker-compose -p my -f local-compose.yml build
	docker-compose -p my -f local-compose.yml up -d
	docker ps
	# debug
	sleep 20
	docker-compose -p my -f local-compose.yml scale mysql=2
	docker ps
