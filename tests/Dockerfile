FROM drydock/u14pyt:prod

RUN apt-get update \
    && apt-get install -y apt-transport-https ca-certificates \
    && echo "deb https://apt.dockerproject.org/repo ubuntu-trusty main" > /etc/apt/sources.list.d/docker.list \
    && apt-key adv --keyserver hkp://p80.pool.sks-keyservers.net:80 --recv-keys 58118E89F3A912897C070ADBF76221572C52609D \
    && apt-get update \
    && apt-get install -y nodejs git docker-engine \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g triton json

RUN git clone --depth 1 --single-branch -b master \
        https://github.com/autopilotpattern/testing.git \
    && cd testing \
    && . $HOME/venv/3.5/bin/activate ; pip install -r requirements.txt \
    && . $HOME/venv/3.5/bin/activate; python setup.py install

COPY tests/tests.py /src/tests.py
COPY bin/manage.py /src/manage.py
COPY docker-compose.yml /src/docker-compose.yml
COPY setup.sh /src/setup.sh
