FROM mysql/mysql-server:5.7.9

RUN yum install -y tar \
    && yum clean all

# get Python driver for MySQL and Consul
RUN curl -Ls -o get-pip.py https://bootstrap.pypa.io/get-pip.py && \
    python get-pip.py && \
    pip install PyMySQL==0.6.7 && \
    pip install python-Consul==0.4.7

# need something for parsing JSON
RUN curl -Lo /usr/bin/jq \
    https://github.com/stedolan/jq/releases/download/jq-1.5/jq-linux64 && \
    chmod +x /usr/bin/jq

# get Containerbuddy release
RUN export CB=containerbuddy-0.0.5 &&\
    curl -Lo /tmp/${CB}.tar.gz \
    https://github.com/joyent/containerbuddy/releases/download/0.0.5/${CB}.tar.gz && \
	tar -xf /tmp/${CB}.tar.gz && \
    mv /containerbuddy /bin/

# configure Containerbuddy and MySQL
COPY bin/* /bin/
COPY etc/* /etc/

# override the parent entrypoint
ENTRYPOINT []

# use --console to get error logs to stderr
CMD [ "/bin/containerbuddy", \
      "mysqld", \
      "--console", \
      "--log-bin=mysql-bin"]
