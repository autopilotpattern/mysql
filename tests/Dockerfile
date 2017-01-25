FROM alpine:3.4

RUN apk update \
    && apk add nodejs docker python3
RUN npm install -g triton json

# install dependencies
COPY tests/testing/requirements.txt /tmp/requirements.txt
COPY tests/testing/testcases.py /src/testcases.py
RUN pip3 install -r /tmp/requirements.txt

# install tests
COPY tests/tests.py /src/tests.py
COPY docker-compose.yml /src/docker-compose.yml
COPY setup.sh /src/setup.sh
