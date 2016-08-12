FROM percona:5.6

ENV CONTAINERPILOT_VER 2.4.0
ENV CONTAINERPILOT file:///etc/containerpilot.json

# By keeping a lot of discrete steps in a single RUN we can clean up after
# ourselves in the same layer. This is gross but it saves ~100MB in the image
RUN set -ex \
    && export buildDeps='python-dev gcc unzip' \
    && export runDeps='python curl libffi-dev libssl-dev percona-xtrabackup ca-certificates' \
    && apt-get update \
    && apt-get install -y $buildDeps $runDeps --no-install-recommends \
    # \
    # get Python drivers MySQL, Consul, and Manta \
    # \
    && curl -Lvo get-pip.py https://bootstrap.pypa.io/get-pip.py \
    && python get-pip.py \
    && pip install \
       PyMySQL==0.6.7 \
       python-Consul==0.4.7 \
       manta==2.5.0 \
    # \
    # Add Consul from https://releases.hashicorp.com/consul \
    # \
    && export CHECKSUM=abdf0e1856292468e2c9971420d73b805e93888e006c76324ae39416edcf0627 \
    && curl -Lvo /tmp/consul.zip "https://releases.hashicorp.com/consul/0.6.4/consul_0.6.4_linux_amd64.zip" \
    && echo "${CHECKSUM}  /tmp/consul.zip" | sha256sum -c \
    && unzip /tmp/consul -d /usr/local/bin \
    && rm /tmp/consul.zip \
    && mkdir /config \
    # \
    # Add ContainerPilot and set its configuration file path \
    # \
    && export CONTAINERPILOT_CHECKSUM=dbdad2cd8da8fe6128f8a2d1736f7b051ba70fe6 \
    && curl -Lvo /tmp/containerpilot.tar.gz "https://github.com/joyent/containerpilot/releases/download/${CONTAINERPILOT_VER}/containerpilot-${CONTAINERPILOT_VER}.tar.gz" \
    && echo "${CONTAINERPILOT_CHECKSUM}  /tmp/containerpilot.tar.gz" | sha1sum -c \
    && tar zxf /tmp/containerpilot.tar.gz -C /usr/local/bin \
    && rm /tmp/containerpilot.tar.gz \
    # \
    # clean up to minimize image layer size \
    # \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get purge -y --auto-remove $buildDeps



# configure ContainerPilot and MySQL
COPY etc/* /etc/
COPY bin/* /usr/local/bin/

# override the parent entrypoint
ENTRYPOINT []

# use --console to get error logs to stderr
CMD [ "containerpilot", \
      "mysqld", \
      "--console", \
      "--log-bin=mysql-bin", \
      "--log_slave_updates=ON", \
      "--gtid-mode=ON", \
      "--enforce-gtid-consistency=ON" \
]
