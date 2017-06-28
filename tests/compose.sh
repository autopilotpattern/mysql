#!/bin/bash
set -e

export GIT_BRANCH="${GIT_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
export TAG="${TAG:-branch-$(basename "$GIT_BRANCH")}"
export COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-my}"
export COMPOSE_FILE="${COMPOSE_FILE:-./examples/compose/docker-compose.yml}"

user=${MYSQL_USER:-dbuser}
passwd=${MYSQL_PASSWORD:-seekretPassword}
db=${MYSQL_DATABASE:-demodb}

project="$COMPOSE_PROJECT"
manifest="$COMPOSE_FILE"


fail() {
    echo
    echo '------------------------------------------------'
    echo 'FAILED: dumping logs'
    echo '------------------------------------------------'
    docker-compose -p "$project" -f "$manifest" ps
    docker-compose -p "$project" -f "$manifest" logs > compose.log
    echo '------------------------------------------------'
    echo 'FAILED'
    echo "$1"
    echo '------------------------------------------------'
    exit 1
}

pass() {
    teardown
    echo
    echo '------------------------------------------------'
    echo 'PASSED!'
    echo
    exit 0
}

function finish {
    result=$?
    if [ $result -ne 0 ]; then fail "unexpected error"; fi
    pass
}
trap finish EXIT



# --------------------------------------------------------------------
# Helpers

# asserts that 'count' MySQL instances are running and marked as Up
# by Docker. fails after the timeout.
wait_for_containers() {
    local count timeout i got
    count="$1"
    timeout="${3:-120}" # default 120sec
    i=0
    echo "waiting for $count MySQL containers to be Up..."
    while [ $i -lt "$timeout" ]; do
        got=$(docker-compose -p "$project" -f "$manifest" ps mysql)
        got=$(echo "$got" | grep -c "Up")
        if [ "$got" -eq "$count" ]; then
            echo "$count instances reported Up in <= $i seconds"
            return
        fi
        i=$((i+1))
        sleep 1
    done
    fail "$count instances did not report Up within $timeout seconds"
}

# asserts that the application has registered at least n instances with
# Consul. fails after the timeout.
wait_for_service() {
    local service count timeout i got consul_ip
    service="$1"
    count="$2"
    timeout="${3:-300}" # default 300sec
    i=0
    echo "waiting for $count instances of $service to be registered with Consul..."
    consul_ip=$(docker inspect "${project}_consul_1" | json -a NetworkSettings.IPAddress)
    while [ $i -lt "$timeout" ]; do
        got=$(curl -s "http://${consul_ip}:8500/v1/health/service/${service}?passing" \
                     | json -a Service.Address | wc -l | tr -d ' ')
        if [ "$got" -eq "$count" ]; then
            echo "$service registered in <= $i seconds"
            return
        fi
        i=$((i+1))
        sleep 1
    done
    fail "waited for service $service for $timeout seconds but it was not registed with Consul"
}

# gets the container that's currently primary in Consul
get_primary() {
    local got consul_ip
    consul_ip=$(docker inspect "${project}_consul_1" | json -a NetworkSettings.IPAddress)
    got=$(curl -s "http://${consul_ip}:8500/v1/health/service/mysql-primary?passing" \
                 | json -a Node.Address)
    echo "$got"
}

# gets a container that's currently a replica in Consul
get_replica() {
    local got consul_ip
    consul_ip=$(docker inspect "${project}_consul_1" | json -a NetworkSettings.IPAddress)
    got=$(curl -s "http://${consul_ip}:8500/v1/health/service/mysql?passing" \
                 | json -a Node.Address)
    echo "$got"
}

# creates a table on the first instance, which will be replicated to
# the other nodes
create_table() {
    echo 'creating test table'
    exec_query "${project}_mysql_1" 'CREATE TABLE tbl1 (field1 INT, field2 VARCHAR(36));'
}

check_replication() {
    echo 'checking replication'
    local primary="$1"
    local replica="$2"
    local testkey="$3"
    local testval="$3"
    echo "writing to $primary"
    exec_query "$primary" "INSERT INTO tbl1 (field1, field2) VALUES ($testkey, \"$testval\");"

    # check the replica, giving it a few seconds to catch up
    local timeout i
    timeout=5
    i=0
    echo "checking read from $replica"
    while [ $i -lt "$timeout" ]; do
        got=$(exec_query "$replica" "SELECT * FROM tbl1 WHERE field1=$testkey;")
        got=$(echo "$got" | grep -c "$testkey: $testval")
        if [ "$got" -eq 1 ]; then
            return
        fi
        i=$((i+1))
        sleep 1
    done
    fail "failed to replicate write from $primary to $replica; query got $got"
}

# runs a SQL statement on the node via docker exec. normally this method
# would be subject to SQL injection but we control all inputs and we don't
# want to have to ship a mysql client in this test rig.
exec_query() {
    local node="$1"
    local query="$2"
    echo "$node"
    out=$(docker exec -i "$node" \
                 mysql -u "$user" "-p${passwd}" --vertical -e "$query" "$db")
    echo "$out"
}

restart() {
    node="${project}_$1"
    docker restart "$node"
}

stop() {
    node="${project}_$1"
    docker stop "$node"
}

run() {
    echo
    echo '* cleaning up previous test run'
    echo
    docker-compose -p "$project" -f "$manifest" stop
    docker-compose -p "$project" -f "$manifest" rm -f

    echo
    echo '* standing up initial test targets'
    echo
    docker-compose -p "$project" -f "$manifest" up -d
}

teardown() {
    echo
    echo '* tearing down containers'
    echo
    docker-compose -p "$project" -f "$manifest" stop
    docker-compose -p "$project" -f "$manifest" rm -f
}

scale() {
    count="$1"
    echo
    echo '* scaling up cluster'
    echo
    docker-compose -p "$project" -f "$manifest" scale mysql="$count"
}


# --------------------------------------------------------------------
# Test sections

test-failover() {
    echo
    echo '------------------------------------------------'
    echo 'executing failover test'
    echo '------------------------------------------------'

    # stand up and setup
    run
    wait_for_containers 1
    wait_for_service 'mysql-primary' 1
    scale 3
    wait_for_containers 3
    wait_for_service 'mysql' 2
    create_table

    # verify working
    check_replication "${project}_mysql_1" "${project}_mysql_2" "1" "a"

    sleep 15

    # force failover and verify again
    stop "mysql_1"
    wait_for_containers 2
    wait_for_service 'mysql-primary' 1
    wait_for_service 'mysql' 1

    local primary replica
    primary=$(get_primary)
    replica=$(get_replica)
    check_replication "$primary" "$replica" "2" "b"
}

# --------------------------------------------------------------------
# Main loop

test-failover
