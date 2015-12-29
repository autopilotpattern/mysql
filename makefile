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
stop-local:
	docker-compose -p my -f local-compose.yml stop || true
	docker-compose -p my -f local-compose.yml rm -f || true

build-local:
	docker-compose -p my -f local-compose.yml build

cleanup:
	-mrm -r /${SDC_ACCOUNT}/stor/triton-mysql/
	mmkdir /${SDC_ACCOUNT}/stor/triton-mysql --role-tag=triton_mysql

test: stop-local build-local
	MANTA_PRIVATE_KEY=`cat manta` docker-compose -p my -f local-compose.yml up -d
	docker ps

standby:
	MANTA_PRIVATE_KEY=`cat manta` docker-compose -p my -f local-compose.yml scale mysql=2
	docker ps

replica:
	MANTA_PRIVATE_KEY=`cat manta` docker-compose -p my -f local-compose.yml scale mysql=3
	docker ps

python-manta:
	git clone git@github.com:tgross/python-manta.git

python-manta-builder:
	head -15 Dockerfile > Dockerfile-builder
	docker build -f Dockerfile-builder -t python-manta-builder .
	rm Dockerfile-builder

python-manta/dist/manta-2.4.1-py2-none-any.whl: python-manta-builder python-manta
	docker run --rm \
		-v `pwd`:/src \
		-w /src/python-manta \
		my_mysql python setup.py bdist_wheel

deps: python-manta/dist/manta-2.4.1-py2-none-any.whl

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
	mmkdir ${SDC_ACCOUNT}/stor/triton-mysql --role-tag=triton_mysql
