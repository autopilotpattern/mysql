# Makefile for shipping the container image and setting up
# permissions in Manta. Building with the docker-compose file
# directly works just fine without this.

MAKEFLAGS += --warn-undefined-variables
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail
.DEFAULT_GOAL := build

MANTA_LOGIN ?= triton_mysql
MANTA_ROLE ?= triton_mysql
MANTA_POLICY ?= triton_mysql

build:
	docker-compose -p my -f local-compose.yml build

ship:
	docker tag -f my_mysql autopilotpattern/mysql
	docker push autopilotpattern/mysql

# -------------------------------------------------------
# testing

DOCKER_CERT_PATH ?=
DOCKER_HOST ?=
DOCKER_TLS_VERIFY ?=
LOG_LEVEL ?= DEBUG

ifeq ($(DOCKER_CERT_PATH),)
	DOCKER_CTX := -v /var/run/docker.sock:/var/run/docker.sock
else
	DOCKER_CTX := -e DOCKER_TLS_VERIFY=$(DOCKER_TLS_VERIFY) -e DOCKER_CERT_PATH=$(DOCKER_CERT_PATH) -e DOCKER_HOST=$(DOCKER_HOST)
endif

cleanup:
	$(call check_var, SDC_ACCOUNT, Required to cleanup Manta.)
	-mrm -r /${SDC_ACCOUNT}/stor/triton-mysql/
	mmkdir /${SDC_ACCOUNT}/stor/triton-mysql
	mchmod -- +triton_mysql /${SDC_ACCOUNT}/stor/triton-mysql

build-test:
	docker build -f tests/Dockerfile -t="test" .

test-triton:
	docker run --rm \
		-e DOCKER_TLS_VERIFY=1 \
		-e DOCKER_CERT_PATH=/.sdc/docker/${SDC_ACCOUNT} \
		-e DOCKER_HOST=$(DOCKER_HOST) \
		-v ${HOME}/.sdc:/.sdc \
		-v $(shell pwd):/src \
		-w /src test python tests/tests.py

test:
	docker run --rm $(DOCKER_CTX) \
		-e LOG_LEVEL=$(LOG_LEVEL) \
		-e COMPOSE_FILE=local-compose.yml \
		-v $(shell pwd):/src \
		-v ${HOME}/src/autopilotpattern/testing/testcases.py:/usr/lib/python2.7/site-packages/testcases.py \
		-w /src test python tests/tests.py

# -------------------------------------------------------

# create user and policies for backups
# you need to have your SDC_ACCOUNT set
# usage:
# make manta EMAIL=example@example.com PASSWORD=strongpassword

manta:
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


# -------------------------------------------------------
# helper functions for testing if variables are defined

check_var = $(foreach 1,$1,$(__check_var))
__check_var = $(if $(value $1),,\
	$(error Missing $1 $(if $(value 2),$(strip $2))))
