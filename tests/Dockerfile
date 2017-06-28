# NOTE: this Dockerfile needs to be run from one-level up so that
# we get the examples docker-compose.yml files. Use 'make build/tester'
# in the makefile at the root of this repo and everything will work

FROM alpine:3.5

RUN apk update \
    && apk add nodejs python3 openssl bash curl docker
RUN npm install -g triton manta json

# the Compose package in the public releases doesn't work on Alpine
RUN pip3 install docker-compose==1.10.0

# install specific version of Docker and Compose client
COPY tests/triton-docker-cli/triton-docker /usr/local/bin/triton-docker
RUN sed -i 's/1.9.0/1.10.0/' /usr/local/bin/triton-docker \
    && ln -s /usr/local/bin/triton-docker /usr/local/bin/triton-compose \
    && ln -s /usr/local/bin/triton-docker /usr/local/bin/triton-docker-install \
    && /usr/local/bin/triton-docker-install \
    && rm /usr/local/bin/triton-compose-helper \
    && ln -s /usr/bin/docker-compose /usr/local/bin/triton-compose-helper


# install test targets
COPY examples/triton/docker-compose.yml /src/examples/triton/docker-compose.yml
COPY examples/triton/setup.sh /src/examples/triton/setup.sh
COPY examples/compose/docker-compose.yml /src/examples/compose/docker-compose.yml
#COPY examples/compose/setup.sh /src/examples/compose/setup.sh

# install test code
COPY tests/triton.sh /src/triton.sh
COPY tests/compose.sh /src/compose.sh
