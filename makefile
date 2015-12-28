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
	MANTA_PRIVATE_KEY=`cat manta` docker-compose -p my -f local-compose.yml up -d
	docker ps
	# debug
	sleep 20
	docker-compose -p my -f local-compose.yml scale mysql=2
	docker ps


# create user and policies for backups
# usage:
# make manta EMAIL=example@example.com PASSWORD=strongpassword
manta:
	ssh-keygen -t rsa -b 4096 -C "${EMAIL}" -f manta
	sdc-user create --login=triton_mysql --password=${PASSWORD} --email=${EMAIL}
	sdc-user upload-key $(ssh-keygen -E md5 -lf ./manta | awk -F' ' '{gsub("MD5:","");{print $2}}') --name=triton-mysql-key triton_mysql ./manta.pub
	sdc-policy create --name=triton_mysql \
		--rules='CAN getobject' \
		--rules='CAN putobject' \
		--rules='CAN putmetadata' \
		--rules='CAN putsnaplink' \
		--rules='CAN getdirectory' \
		--rules='CAN putdirectory'
	sdc-role create --name=triton_mysql \
		--policies=triton_mysql \
		--members=triton_mysql
