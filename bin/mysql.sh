#!/bin/bash
set -e

PID=
CONSUL=${CONSUL:-consul}

# set up environment and render my.cnf config
config() {

    # replace thread_concurrency value from environment or use a sensible
    # default (estimating 1/2 RAM in GBs as the number of CPUs and 2 threads
    # per CPU)
    local default=$(awk '/MemTotal/{printf "%.0f\n", ($2 / 1024 / 1024)}' /proc/meminfo)
    local conc=$(printf 's/^thread_concurrency = .*$/thread_concurrency = %s/' \
                        ${THREAD_CONCURRENCY:-${default}})
    sed -i "${conc}" /etc/my.cnf

    # replace innodb_buffer_pool_size value from environment
    # or use a sensible default (70% of available physical memory)
    local default=$(awk '/MemTotal/{printf "%.0f\n", ($2 / 1024) * 0.7}' /proc/meminfo)M
    local buffer=$(printf 's/^innodb_buffer_pool_size = .*$/innodb_buffer_pool_size = %s/' \
                          ${INNODB_BUFFER_POOL_SIZE:-${default}})
    sed -i "${buffer}" /etc/my.cnf

    # replace server-id with ID derived from hostname
    # ref https://dev.mysql.com/doc/refman/5.7/en/replication-configuration.html
    local id=$(hostname | python -c 'import sys; print(int(str(sys.stdin.read())[:4], 16))')
    sed -i $(printf 's/^server-id=.*$/server-id=%s/' $id) /etc/my.cnf
    sed -i $(printf 's/^report-host=.*$/report-host=%s/' $(hostname)) /etc/my.cnf

    # hypothetically we could start the container with `--datadir` set
    # but there's no reason to jump thru those hoops in this environment
    # and it complicates the entrypoint greatly
    DATADIR=$(mysqld --verbose --help --log-bin-index=/tmp/tmp.index | awk '$1 == "datadir" { print $2 }')

}

# make sure we have all required environment variables
checkConfig() {
    if [ -z "$MYSQL_ROOT_PASSWORD" -a -z "$MYSQL_ALLOW_EMPTY_PASSWORD" -a -z "$MYSQL_RANDOM_ROOT_PASSWORD" ]; then
        echo >&2 'error: database is uninitialized and password option is not specified '
        echo >&2 '  You need to specify one of MYSQL_ROOT_PASSWORD, MYSQL_ALLOW_EMPTY_PASSWORD and MYSQL_RANDOM_ROOT_PASSWORD'
        exit 1
    fi
}

# clean up the temporary mysqld service
cleanup() {
    if [[ -n $PID ]]; then
        echo 'Shutting down temporary bootstrap mysqld (PID ' $PID ')'
        if ! kill -s TERM "$PID" || ! wait "$PID"; then
            echo >&2 'MySQL init process failed.'
            exit 1
        fi
    fi
}

# ---------------------------------------------------------
# Initialization

initializeDb() {
    mkdir -p "${DATADIR}"
    chown -R mysql:mysql "${DATADIR}"

    echo 'Initializing database...'
    mysqld --initialize-insecure=on --user=mysql --datadir="${DATADIR}"
    echo 'Database initialized.'
}

bootstrapDb() {
    mysqld --user=mysql --datadir="${DATADIR}" --skip-networking --skip-slave-start &
    PID="$!"
    echo 'Running temporary bootstrap mysqld (PID ' $PID ')'
}

waitForConnection() {
    mysql=( mysql --protocol=socket -uroot )
    echo 'Waiting for bootstrap mysqld to start...'
    for i in {30..0}; do
        if echo 'SELECT 1' | "${mysql[@]}" &> /dev/null; then
            break
        fi
        echo -ne '.'
        sleep 1
    done
    if [ "$i" = 0 ]; then
        echo >&2 'MySQL init process failed.'
        exit 1
    fi
    echo
}

setupRootUser() {
    if [ ! -z "$MYSQL_RANDOM_ROOT_PASSWORD" ]; then
        MYSQL_ROOT_PASSWORD="$(pwmake 128)"
        echo "GENERATED ROOT PASSWORD: $MYSQL_ROOT_PASSWORD"
    fi

    "${mysql[@]}" <<-EOSQL
		SET @@SESSION.SQL_LOG_BIN=0;
		DELETE FROM mysql.user where user != 'mysql.sys';
		CREATE USER 'root'@'%' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}' ;
		GRANT ALL ON *.* TO 'root'@'%' WITH GRANT OPTION ;
		DROP DATABASE IF EXISTS test ;
		FLUSH PRIVILEGES ;
EOSQL
    if [ ! -z "$MYSQL_ROOT_PASSWORD" ]; then
        mysql+=( -p"${MYSQL_ROOT_PASSWORD}" )
    fi
}

createDb() {
    if [ "$MYSQL_DATABASE" ]; then
        echo "CREATE DATABASE IF NOT EXISTS \`$MYSQL_DATABASE\` ;" | "${mysql[@]}"
        mysql+=( "$MYSQL_DATABASE" )
    fi
}

createDefaultUser() {
    if [ "$MYSQL_USER" -a "$MYSQL_PASSWORD" ]; then
        echo "CREATE USER '"$MYSQL_USER"'@'%' IDENTIFIED BY '"$MYSQL_PASSWORD"' ;" \
            | "${mysql[@]}"
        if [ "$MYSQL_DATABASE" ]; then
            echo "GRANT ALL ON \`"$MYSQL_DATABASE"\`.* TO '"$MYSQL_USER"'@'%' ;" | "${mysql[@]}"
        fi
        echo 'FLUSH PRIVILEGES ;' | "${mysql[@]}"
    fi
}

createReplUser() {
    if [ "$MYSQL_REPL_USER" -a "$MYSQL_REPL_PASSWORD" ]; then
        "${mysql[@]}" <<-EOSQL
			CREATE USER '$MYSQL_REPL_USER'@'%' IDENTIFIED BY '$MYSQL_REPL_PASSWORD';
			GRANT REPLICATION SLAVE ON *.* TO '$MYSQL_REPL_USER'@'%';
			FLUSH PRIVILEGES ;
EOSQL
    fi
}

# reworked from Oracle-provided Docker image:
# https://github.com/mysql/mysql-docker/blob/mysql-server/5.7/docker-entrypoint.sh
init() {

    if [ -d "${DATADIR}/mysql" ]; then
        checkConfig        # make sure we have all required environment variables
        bootstrapDb        # sets $PID for temporary mysqld while we bootstrap
        waitForConnection  # sets $mysql[] which we'll use for all future conns
        stopReplica
    else
        checkConfig        # make sure we have all required environment variables
        initializeDb       # sets datadir
        bootstrapDb        # sets $PID for temporary mysqld while we bootstrap
        waitForConnection  # sets $mysql[] which we'll use for all future conns

        mysql_tzinfo_to_sql /usr/share/zoneinfo | "${mysql[@]}" mysql

        setupRootUser      # creates root user
        createDb           # create the default DB
        createDefaultUser  # create the default DB user
        createReplUser     # create the replication user

        # run user-defined files added to /etc/initdb.d in a child Docker image
        for f in /etc/initdb.d/*; do
            case "$f" in
                *.sh)  echo "$0: running $f"; . "$f" ;;
                *.sql) echo "$0: running $f"; "${mysql[@]}" < "$f" && echo ;;
                *)     echo "$0: ignoring $f" ;;
            esac
        done

        # remove the one-time root password (default behavior)
        if [ ! -z "$MYSQL_ONETIME_PASSWORD" ]; then
            "${mysql[@]}" <<-EOSQL
                ALTER USER 'root'@'%' PASSWORD EXPIRE;
EOSQL
        fi

        echo
        echo 'MySQL init process done. Ready for start up.'
        echo
    fi
}


# ---------------------------------------------------------
# Replication

# Query Consul for the PRIMARY_NODE key
getPrimaryNode() {
    PRIMARY_NODE=$(curl -s http://${CONSUL}:8500/v1/kv/mysql-primary | \
                          jq -r '.[].Value' | base64 --decode)
}

# Query Consul for healthy mysql nodes and check their ServiceID vs
# PRIMARY_NODE. Sets PRIMARY_HOST to the IP address of a matching node.
getPrimaryHost() {

    if [[ -z ${PRIMARY_NODE} ]]; then
        getPrimaryNode
    fi

    echo "Checking if primary (${PRIMARY_NODE}) is healthy..."

    # providing a 30 second timeout here to avoid bootstrap failures on
    # transient network paritions
    for i in {30..0}; do
        PRIMARY_HOST=$(curl -Ls --fail http://${CONSUL}:8500/v1/catalog/service/mysql?passing \
            | jq -r 'map(select(.ServiceID == "'${PRIMARY_NODE}'")) | .[0].ServiceAddress')
        if [[ ${PRIMARY_HOST} != "null" ]] && [[ -n ${PRIMARY_HOST} ]]; then
            echo 'Found primary at:' ${PRIMARY_HOST}
            break
        fi
        echo -ne '.'
        sleep 1
    done
}

# returns "true" on success or "false" on failure due to CAS
markSelfPrimary() {
    PRIMARY_NODE=mysql-$(hostname)
    echo "Marking ${PRIMARY_NODE} as primary..."
    for i in {30..0}; do
        local ok=$(curl -XPUT -s --fail -d ${PRIMARY_NODE} \
                        "http://${CONSUL}:8500/v1/kv/mysql-primary?cas=0" | jq -r '.')
        if [[ ${ok} = "true" ]]; then
            echo
            break
        fi
        if [[ ${ok} = "false" ]]; then
            # The only way this scenario appears is if we raced with
            # another node. So we're going to just start over.
            echo 'Tried to set this instance as primary but primary exists.'
            echo 'Restarting bootstrap process.'
            onStart
        fi
        echo -ne '.'
        sleep 1
    done
}

# Set up GITD-based replication to the primary; once this is set the
# replica will automatically try to catch up with the primary by pulling
# its entire binlog. This is comparatively slow but suitable for small
# databases.
setPrimaryForReplica() {
    "${mysql[@]}" <<-EOSQL
		CHANGE MASTER TO
		MASTER_HOST           = '$PRIMARY_HOST',
		MASTER_USER           = '$MYSQL_REPL_USER',
		MASTER_PASSWORD       = '$MYSQL_REPL_PASSWORD',
		MASTER_PORT           = 3306,
		MASTER_CONNECT_RETRY  = 60,
        MASTER_AUTO_POSITION  = 1,
		MASTER_SSL            = 0;
        START SLAVE;
EOSQL

}

stopReplica() {
    echo 'STOP SLAVE;' | "${mysql[@]}"
}

replica() {
    checkConfig        # make sure we have all required environment variables
    initializeDb       # sets datadir
    bootstrapDb        # $PID for temporary mysqld while we bootstrap
    waitForConnection  # sets $mysql[] which we'll use for all future conns

    mysql_tzinfo_to_sql /usr/share/zoneinfo | "${mysql[@]}" mysql

    echo 'Setting up replication.'

    # belt-and-suspenders in the workflow so that if we call this
    # script with `replica` as the command we assert environment
    if [[ -z ${PRIMARY_HOST} ]]; then
        getPrimaryHost
        if [[ -z ${PRIMARY_HOST} ]]; then
            echo 'Tried replication setup, but primary is set and not healthy. Exiting!'
            exit 1
        fi
    fi

    setPrimaryForReplica
    cleanup
}


# TODO: this section will be for cases where we have a large existing
# dataset that we need to update from the master before starting
# replication. This way we avoid running thru the entire binlog, but
# it requires outside intervention that we won't be able to do easily
# just via docker-compose

# dataDump() {
#     mysqldump -h ${PRIMARY_HOST} -p 3306 --all-databases --master-data --set-gtid-purged=ON > dbdump.db
# }

# importDump() {
#     mysql < dbdump.db
#     mysqlbinlog --read-from-remote-server --read-from-remote-master
# }

# startReadOnly() {
#     echo 'SET @@global.read_only = ON;' | mysql
# }

# stopReadOnly() {
#     echo 'SET @@global.read_only = OFF;' | mysql
# }



# ---------------------------------------------------------
# Containerbuddy-called functions

# If Consul knows about the primary already, either setup replication
# for that primary or exit if it's not healthy. Otherwise, set this
# node up as a new primary, including initial setup if required.
onStart() {
    getPrimaryNode # will set ${PRIMARY_NODE}
    if [[ -n ${PRIMARY_NODE} ]]; then
        getPrimaryHost
        if [[ -n ${PRIMARY_HOST} ]]; then
            replica
            exit 0
        else
            echo 'Primary is set but not healthy. Exiting!'
            exit 1
        fi
    else
        markSelfPrimary
        init
        cleanup
        exit 0
    fi
}

health() {
    mysql=( mysql --protocol=socket -u ${MYSQL_USER} -p${MYSQL_PASSWORD})
    if echo 'SELECT 1' | "${mysql[@]}" &> /dev/null; then
        exit 0
    fi
    exit 1
}

# TODO:
# this will be where we hook-in failover behaviors
onChange() {
    echo 'onChange'
}


# ---------------------------------------------------------
# default behavior will be to start mysqld, running the
# initialization if required

# write config file, even if we've previously initialized the DB,
# so that we can account for changed hostnamed, resized containers, etc.
config

# make sure that if we've pulled in an external data volume that
# the mysql user can read it
chown -R mysql:mysql "${DATADIR}"

# if command starts with an option, prepend mysqld
if [ "${1:0:1}" = '-' ]; then
    set -- mysqld "$@"
fi

# here's where we'll divert from running mysqld if we want
# to set up replication, perform backups, etc.
cmd=$1
if [ ! -z "$cmd" ]; then
    if ! type $cmd > /dev/null; then
        # whatever we're trying to run is external to this script
        exec "$@"
    else
        shift 1
        $cmd "$@"
    fi
    exit
fi

# default behavior: set up the DB
onStart
