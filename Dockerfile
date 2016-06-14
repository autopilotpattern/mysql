FROM percona:5.6

RUN apt-get update \
    && apt-get install -y \
        python \
        python-dev \
        gcc \
        curl \
        libffi-dev \
        libssl-dev \
        percona-xtrabackup \
    && rm -rf /var/lib/apt/lists/*

# get Python drivers MySQL, Consul, and Manta
RUN curl -Ls -o get-pip.py https://bootstrap.pypa.io/get-pip.py && \
    python get-pip.py && \
    pip install \
        PyMySQL==0.6.7 \
        python-Consul==0.4.7 \
        manta==2.5.0 \
        mock==2.0.0

# Add ContainerPilot and set its configuration file path
ENV CONTAINERPILOT_VER 2.0.1
ENV CONTAINERPILOT file:///etc/containerpilot.json
RUN export CONTAINERPILOT_CHECKSUM=a4dd6bc001c82210b5c33ec2aa82d7ce83245154 \
    && curl -Lso /tmp/containerpilot.tar.gz \
        "https://github.com/joyent/containerpilot/releases/download/${CONTAINERPILOT_VER}/containerpilot-${CONTAINERPILOT_VER}.tar.gz" \
    && echo "${CONTAINERPILOT_CHECKSUM}  /tmp/containerpilot.tar.gz" | sha1sum -c \
    && tar zxf /tmp/containerpilot.tar.gz -C /usr/local/bin \
    && rm /tmp/containerpilot.tar.gz

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
