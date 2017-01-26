FROM alpine:3.4

RUN apk update \
    && apk add nodejs docker python3
RUN npm install -g triton json

# install dependencies
RUN pip3 install \
    docker-compose==1.10.0 \
    python-Consul==0.4.7 \
    IPy==0.83

# install tests
COPY tests/testing/testcases.py /src/testcases.py
COPY tests/tests.py /src/tests.py
COPY docker-compose.yml /src/docker-compose.yml
COPY setup.sh /src/setup.sh
