FROM mysql/mysql-server:5.7.9

RUN yum install -y tar \
    && yum clean all

# need something for parsing JSON
RUN curl -Lo /usr/bin/jq \
    https://github.com/stedolan/jq/releases/download/jq-1.5/jq-linux64 && \
    chmod +x /usr/bin/jq

# get Containerbuddy release
RUN export CB=containerbuddy-0.0.3 &&\
    mkdir -p /opt/containerbuddy && \
    curl -Lo /tmp/${CB}.tar.gz \
    https://github.com/joyent/containerbuddy/releases/download/0.0.3/${CB}.tar.gz && \
	tar -xf /tmp/${CB}.tar.gz && \
    mv /build/containerbuddy /opt/containerbuddy/

# configure Containerbuddy and MySQL
COPY bin/* /opt/containerbuddy/
COPY etc/* /etc/

# override the parent entrypoint
ENTRYPOINT []

# use --console to get error logs to stderr
# send slow query logs to table???
CMD [ "/opt/containerbuddy/containerbuddy", \
      "mysqld", \
      "--console", \
      "--log-bin=mysql-bin"]
