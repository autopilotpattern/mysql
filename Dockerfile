FROM percona:5.6

RUN apt-get update \
    && apt-get install -y \
    python \
    python-dev \
    gcc \
    curl \
    percona-xtrabackup \
    && rm -rf /var/lib/apt/lists/*

# get Python drivers MySQL, Consul, and Manta
RUN curl -Ls -o get-pip.py https://bootstrap.pypa.io/get-pip.py && \
    python get-pip.py && \
    pip install \
        PyMySQL==0.6.7 \
        python-Consul==0.4.7 \
        manta==2.5.0

# get Containerpilot release
RUN export CP_VERSION=2.0.1 &&\
   curl -Lo /tmp/containerpilot.tar.gz \
   https://github.com/joyent/containerpilot/releases/download/${CP_VERSION}/containerpilot-${CP_VERSION}.tar.gz && \
   tar -xzf /tmp/containerpilot.tar.gz && \
   mv /containerpilot /bin/

# configure Containerpilot and MySQL
COPY bin/* /bin/
COPY etc/* /etc/

# override the parent entrypoint
ENTRYPOINT []

# use --console to get error logs to stderr
CMD [ "/bin/containerpilot", \
      "mysqld", \
      "--console", \
      "--log-bin=mysql-bin", \
      "--log_slave_updates=ON", \
      "--gtid-mode=ON", \
      "--enforce-gtid-consistency=ON" \
]
