# Makefile for shipping the container image and setting up
# permissions in Manta. Building with the docker-compose file
# directly works just fine without this.

MAKEFLAGS += --warn-undefined-variables
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail
.DEFAULT_GOAL := build
.PHONY: build ship test help

MANTA_LOGIN ?= triton_mysql
MANTA_ROLE ?= triton_mysql
MANTA_POLICY ?= triton_mysql

## Display this help message
help:
	@awk '/^##.*$$/,/[a-zA-Z_-]+:/' $(MAKEFILE_LIST) | awk '!(NR%2){print $$0p}{p=$$0}' | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}' | sort

# ------------------------------------------------
# Container builds

# we get these from shippable if available, otherwise from git
COMMIT ?= $(shell git rev-parse --short HEAD)
BRANCH ?= $(shell git rev-parse --abbrev-ref HEAD)
TAG := $(BRANCH)-$(COMMIT)

## Builds the application container image
build:
	docker build -t=autopilotpattern/mysql:$(TAG) .

tag:
	docker tag autopilotpattern/mysql:$(TAG) autopilotpattern/mysql:latest

## Pushes the application container image to the Docker Hub
ship: tag
	docker push autopilotpattern/mysql:$(TAG)
	docker push autopilotpattern/mysql:latest


# ------------------------------------------------
# Test running

LOG_LEVEL ?= INFO
KEY := ~/.ssh/TritonTestingKey

# if you pass `TRACE=1` into the call to `make` then the Python tests will
# run under the `trace` module (provides detailed call logging)
PYTHON := $(shell if [ -z ${TRACE} ]; then echo "python"; else echo "python -m trace"; fi)

# sets up the Docker context for running the build container locally
# `make test` runs in the build container on Shippable where the boot script
# will pull the source from GitHub first, but if we want to debug locally
# we'll need to mount the local source into the container.
# TODO: remove mount to ~/src/autopilotpattern/testing
LOCALRUN := \
	-e PATH=/root/venv/3.5/bin:/usr/bin:/bin \
	-e COMPOSE_HTTP_TIMEOUT=300 \
	-e LOG_LEVEL=$(LOG_LEVEL) \
	-w /src \
	-v ~/.triton:/root/.triton \
	-v ~/src/autopilotpattern/testing/testcases.py:/root/venv/3.5/lib/python3.5/site-packages/testcases.py \
	-v $(shell pwd)/tests/tests.py:/src/tests.py \
	-v $(shell pwd)/docker-compose.yml:/src/docker-compose.yml \
	-v $(shell pwd)/local-compose.yml:/src/local-compose.yml \
	-v $(shell pwd)/makefile:/src/makefile \
	-v $(shell pwd)/setup.sh:/src/setup.sh \
	-v ~/.ssh/timgross-joyent_id_rsa:/tmp/ssh/TritonTestingKey \
	joyent/test

MANTA_CONFIG := \
	-e MANTA_USER=timgross \
	-e MANTA_SUBUSER=triton_mysql \
	-e MANTA_ROLE=triton_mysql

## Build the test running container
test-runner:
	docker build -f tests/Dockerfile -t="joyent/test" .

# configure triton profile
~/.triton/profiles.d/us-sw-1.json:
	{ \
	  cp /tmp/ssh/TritonTestingKey $(KEY) ;\
	  ssh-keygen -y -f $(KEY) > $(KEY).pub ;\
	  FINGERPRINT=$$(ssh-keygen -l -f $(KEY) | awk '{print $$2}' | sed 's/MD5://') ;\
	  printf '{"url": "https://us-sw-1.api.joyent.com", "name": "TritonTesting", "account": "timgross", "keyId": "%s"}' $${FINGERPRINT} > profile.json ;\
	}
	cat profile.json | triton profile create -f -
	-rm profile.json

# TODO: replace this user w/ a user specifically for testing
## Run the build container (on Shippable) and deploy tests on Triton
test: ~/.triton/profiles.d/us-sw-1.json
	cp tests/tests.py . && \
		DOCKER_TLS_VERIFY=1 \
		DOCKER_CERT_PATH=/root/.triton/docker/timgross@us-sw-1_api_joyent_com \
		DOCKER_HOST=tcp://us-sw-1.docker.joyent.com:2376 \
		COMPOSE_HTTP_TIMEOUT=300 \
		PATH=/root/venv/3.5/bin:/usr/bin:/bin \
		MANTA_USER=timgross \
		MANTA_SUBUSER=triton_mysql \
		MANTA_ROLE=triton_mysql \
		$(PYTHON) tests.py

## Run the build container locally and deploy tests on Triton.
test-local-triton: ~/.triton/profiles.d/us-sw-1.json
	docker run -it --rm \
		-e DOCKER_TLS_VERIFY=1 \
		-e DOCKER_CERT_PATH=/root/.triton/docker/timgross@us-sw-1_api_joyent_com \
		-e DOCKER_HOST=tcp://us-sw-1.docker.joyent.com:2376 \
		$(MANTA_CONFIG) \
		$(LOCALRUN) $(PYTHON) tests.py

## Run the build container locally and deploy tests on local Docker too.
test-local-docker:
	docker run -it --rm \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-e TRITON_SETUP_CERT_PATH=/root/.triton/docker/timgross@us-sw-1_api_joyent_com \
		-e TRITON_SETUP_HOST=tcp://us-sw-1.docker.joyent.com:2376 \
		-e COMPOSE_FILE=local-compose.yml \
		$(MANTA_CONFIG) \
		$(LOCALRUN) $(PYTHON) tests.py

## Run the unit tests inside the mysql container
unit-test:
	docker run --rm -w /usr/local/bin \
		-e LOG_LEVEL=DEBUG \
		-v $(shell pwd)/bin/manage.py:/usr/local/bin/manage.py \
		-v $(shell pwd)/bin/test.py:/usr/local/bin/test.py \
		autopilotpattern/mysql:$(TAG) \
		python test.py

shell:
	docker run -it --rm \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-e COMPOSE_FILE=local-compose.yml \
		$(LOCALRUN) /bin/bash #$(PYTHON)

# -------------------------------------------------------

## Create user and policies for Manta backups
manta:
	# you need to have your SDC_ACCOUNT set
	# usage:
	# make manta EMAIL=example@example.com PASSWORD=strongpassword
	$(call check_var, EMAIL PASSWORD SDC_ACCOUNT, \
		Required to create a Manta login.)

	ssh-keygen -t rsa -b 4096 -C "${EMAIL}" -f manta
	sdc-user create --login=${MANTA_LOGIN} --password=${PASSWORD} --email=${EMAIL}
	sdc-user upload-key $(ssh-keygen -E md5 -lf ./manta | awk -F' ' '{gsub("MD5:","");{print $2}}') --name=${MANTA_LOGIN}-key ${MANTA_LOGIN} ./manta.pub
	sdc-policy create --name=${MANTA_POLICY} \
		--rules='CAN getobject' \
		--rules='CAN putobject' \
		--rules='CAN putmetadata' \
		--rules='CAN putsnaplink' \
		--rules='CAN getdirectory' \
		--rules='CAN putdirectory'
	sdc-role create --name=${MANTA_ROLE} \
		--policies=${MANTA_POLICY} \
		--members=${MANTA_LOGIN}
	mmkdir ${SDC_ACCOUNT}/stor/${MANTA_LOGIN}
	mchmod -- +triton_mysql /${SDC_ACCOUNT}/stor/${MANTA_LOGIN}

## Cleans out Manta backups
cleanup:
	$(call check_var, SDC_ACCOUNT, Required to cleanup Manta.)
	-mrm -r /${SDC_ACCOUNT}/stor/triton-mysql/
	mmkdir /${SDC_ACCOUNT}/stor/triton-mysql
	mchmod -- +triton_mysql /${SDC_ACCOUNT}/stor/triton-mysql


# -------------------------------------------------------
# helper functions for testing if variables are defined

check_var = $(foreach 1,$1,$(__check_var))
__check_var = $(if $(value $1),,\
	$(error Missing $1 $(if $(value 2),$(strip $2))))
