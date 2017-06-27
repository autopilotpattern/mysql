# Makefile for shipping the container image and setting up
# permissions in Manta. Building with the docker-compose file
# directly works just fine without this.

MAKEFLAGS += --warn-undefined-variables
.DEFAULT_GOAL := build
.PHONY: *

# we get these from CI environment if available, otherwise from git
GIT_COMMIT ?= $(shell git rev-parse --short HEAD)
GIT_BRANCH ?= $(shell git rev-parse --abbrev-ref HEAD)

namespace ?= autopilotpattern
tag := branch-$(shell basename $(GIT_BRANCH))
image := $(namespace)/mysql
testImage := $(namespace)/mysql-testrunner

## Display this help message
help:
	@awk '/^##.*$$/,/[a-zA-Z_-]+:/' $(MAKEFILE_LIST) | awk '!(NR%2){print $$0p}{p=$$0}' | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}' | sort

# ------------------------------------------------
# Target environment configuration

# if you pass `TRACE=1` into the call to `make` then the Python tests will
# run under the `trace` module (provides detailed call logging)
ifndef TRACE
python := python
else
python := python -m trace
endif

# ------------------------------------------------
# Container builds

## Builds the application container image locally
build: build/tester
	docker build -t=$(image):$(tag) .

## Build the test running container
build/tester:
	docker build -f tests/Dockerfile -t=$(testImage):$(tag) .

## Push the current application container images to the Docker Hub
push:
	docker push $(image):$(tag)
	docker push $(testImage):$(tag)

## Tag the current images as 'latest' and push them to the Docker Hub
ship:
	docker tag $(image):$(tag) $(image):latest
	docker tag $(testImage):$(tag) $(testImage):latest
	docker tag $(image):$(tag) $(image):latest
	docker push $(image):$(tag)
	docker push $(image):latest


# ------------------------------------------------
# Run the example stack

## Run the stack under local Compose
run/compose:
	cd examples/compose && TAG=$(tag) docker-compose -p my up -d
	cd examples/compose && TAG=$(tag) docker-compose -p my logs -f mysql

## Scale up the local Compose stack
run/compose/scale:
	cd examples/compose && TAG=$(tag) docker-compose -p my scale mysql=2

# ------------------------------------------------
# Test running

## Pull the container images from the Docker Hub
pull:
	docker pull $(image):$(tag)

## Run all tests
test: test/unit test/triton # test/compose

## Run the unit tests inside the mysql container
test/unit:
	docker run --rm -w /usr/local/bin \
		-e LOG_LEVEL=DEBUG \
		$(image):$(tag) \
		$(python) test.py

## Run the unit tests with source mounted to the container for local dev
test/unit-src:
	docker run --rm  -w /usr/local/bin \
		-v $(shell pwd)/bin/manager:/usr/local/bin/manager \
		-v $(shell pwd)/bin/manage.py:/usr/local/bin/manage.py \
		-v $(shell pwd)/bin/test.py:/usr/local/bin/test.py \
		-e LOG_LEVEL=DEBUG \
		$(image):$(tag) \
		$(python) test.py

## Run the integration test runner against Compose locally.
test/compose:
	docker run --rm \
		-e TAG=$(tag) \
		-e GIT_BRANCH=$(GIT_BRANCH) \
		-e WORK_DIR=/src \
		--network=bridge \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-v $(shell pwd)/tests/compose.sh:/src/compose.sh \
		-w /src \
		$(testImage):$(tag) /src/compose.sh


test/shell:
	docker run --rm -it \
		-e TAG=$(tag) \
		-e GIT_BRANCH=$(GIT_BRANCH) \
		-e WORK_DIR=/src \
		--network=bridge \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-v $(shell pwd)/tests/compose.sh:/src/compose.sh \
		-w /src \
		$(testImage):$(tag) /bin/bash

## Run the integration test runner. Runs locally but targets Triton.
test/triton:
	$(call check_var, TRITON_PROFILE, MANTA_USER, MANTA_KEY_ID \
		required to run integration tests on Triton.)
	docker run --rm \
		-e TAG=$(tag) \
		-e TRITON_PROFILE=$(TRITON_PROFILE) \
		-e MANTA_USER=$(MANTA_USER) \
		-e MANTA_KEY_ID=$(MANTA_KEY_ID) \
		-e GIT_BRANCH=$(GIT_BRANCH) \
		-v ~/.ssh:/root/.ssh:ro \
		-v ~/.triton/profiles.d:/root/.triton/profiles.d:ro \
		-w /src \
		$(testImage):$(tag) /src/triton.sh

# -------------------------------------------------------

## Tear down all project containers
teardown:
	docker-compose -p my stop
	docker-compose -p my rm -f

## Dump logs for each container to local disk
logs:
	docker logs my_consul_1 > consul1.log 2>&1
	docker logs my_mysql_1 > mysql1.log 2>&1
	docker logs my_mysql_2 > mysql2.log 2>&1
	docker logs my_mysql_3 > mysql3.log 2>&1

# -------------------------------------------------------

MANTA_URL ?= https://us-east.manta.joyent.com
MANTA_USER ?= triton_mysql
MANTA_SUBUSER ?= triton_mysql
MANTA_LOGIN ?= triton_mysql
MANTA_ROLE ?= triton_mysql
MANTA_POLICY ?= triton_mysql

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

## Cleanup local backups and log debris
clean:
	rm -rf tmp/
	find . -name '*.log' -delete

## Print environment for build debugging
debug:
	@echo GIT_COMMIT=$(GIT_COMMIT)
	@echo GIT_BRANCH=$(GIT_BRANCH)
	@echo namespace=$(namespace)
	@echo tag=$(tag)
	@echo image=$(image)
	@echo testImage=$(testImage)

check_var = $(foreach 1,$1,$(__check_var))
__check_var = $(if $(value $1),,\
	$(error Missing $1 $(if $(value 2),$(strip $2))))
