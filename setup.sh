#!/bin/bash
set -e -o pipefail

help() {
    echo 'Usage ./setup.sh [-f docker-compose.yml] [-p project]'
    echo
    echo 'Checks that your Triton and Docker environment is sane and configures'
    echo 'an environment file to use.'
    echo
    echo 'Optional flags:'
    echo '  -f <filename>   use this file as the docker-compose config file'
    echo '  -p <project>    use this name as the project prefix for docker-compose'
}


# default values which can be overriden by -f or -p flags
export COMPOSE_PROJECT_NAME=mysql
export COMPOSE_FILE=

# give the docker remote api more time before timeout
export COMPOSE_HTTP_TIMEOUT=300

# populated by `check` function whenever we're using Triton
TRITON_USER=
TRITON_DC=
TRITON_ACCOUNT=

# ---------------------------------------------------
# Top-level commmands


# Check for correct configuration
check() {

    command -v docker >/dev/null 2>&1 || {
        echo
        tput rev  # reverse
        tput bold # bold
        echo 'Docker is required, but does not appear to be installed.'
        tput sgr0 # clear
        echo 'See https://docs.joyent.com/public-cloud/api-access/docker'
        exit 1
    }
    command -v json >/dev/null 2>&1 || {
        echo
        tput rev  # reverse
        tput bold # bold
        echo 'Error! JSON CLI tool is required, but does not appear to be installed.'
        tput sgr0 # clear
        echo 'See https://apidocs.joyent.com/cloudapi/#getting-started'
        exit 1
    }

    # if we're not testing on Triton, don't bother checking Triton config
    if [ ! -z "${COMPOSE_FILE}" ]; then
        exit 0
    fi

    command -v triton >/dev/null 2>&1 || {
        echo
        tput rev  # reverse
        tput bold # bold
        echo 'Error! Joyent Triton CLI is required, but does not appear to be installed.'
        tput sgr0 # clear
        echo 'See https://www.joyent.com/blog/introducing-the-triton-command-line-tool'
        exit 1
    }

    # make sure Docker client is pointed to the same place as the Triton client
    local docker_user=$(docker info 2>&1 | awk -F": " '/SDCAccount:/{print $2}')
    local docker_dc=$(echo $DOCKER_HOST | awk -F"/" '{print $3}' | awk -F'.' '{print $1}')
    TRITON_USER=$(triton profile get | awk -F": " '/account:/{print $2}')
    TRITON_DC=$(triton profile get | awk -F"/" '/url:/{print $3}' | awk -F'.' '{print $1}')
    TRITON_ACCOUNT=$(triton account get | awk -F": " '/id:/{print $2}')
    if [ ! "$docker_user" = "$TRITON_USER" ] || [ ! "$docker_dc" = "$TRITON_DC" ]; then
        echo
        tput rev  # reverse
        tput bold # bold
        echo 'Error! The Triton CLI configuration does not match the Docker CLI configuration.'
        tput sgr0 # clear
        echo
        echo "Docker user: ${docker_user}"
        echo "Triton user: ${TRITON_USER}"
        echo "Docker data center: ${docker_dc}"
        echo "Triton data center: ${TRITON_DC}"
        exit 1
    fi

    local triton_cns_enabled=$(triton account get | awk -F": " '/cns/{print $2}')
    if [ ! "true" == "$triton_cns_enabled" ]; then
        echo
        tput rev  # reverse
        tput bold # bold
        echo 'Error! Triton CNS is required and not enabled.'
        tput sgr0 # clear
        echo
        exit 1
    fi

    # setup environment file
    echo '# Environment variables for MySQL service' > _env
    echo 'MYSQL_USER=' >> _env
    echo 'MYSQL_PASSWORD=' >> _env
    echo 'MYSQL_REPL_USER=' >> _env
    echo 'MYSQL_REPL_PASSWORD=' >> _env
    echo 'MYSQL_DATABASE=' >> _env
    echo >> _env

    echo '# Environment variables for backups to Manta' > _env
    echo 'MANTA_BUCKET=' >> _env
    echo 'MANTA_URL=' >> _env
    echo 'MANTA_USER=' >> _env
    echo 'MANTA_SUBUSER=' >> _env
    echo 'MANTA_ROLE=' >> _env
    echo 'MANTA_KEY_ID=' >> _env
    echo 'MANTA_PRIVATE_KEY=' >> _env

    # munge the private key so that we can pass it into an env var sanely
    # and then unmunge it in our startup script
    echo MANTA_PRIVATE_KEY=$(cat ${MANTA_PRIVATE_KEY}/key.pem | tr '\n' '#') >> _env
    echo >> _env

	# Consul discovery via Triton CNS
    echo CONSUL=consul.svc.${TRITON_ACCOUNT}.${TRITON_DC}.cns.joyent.com >> _env
    echo >> _env

    echo 'Edit the _env file with your desired MYSQL_* and MANTA_* config'
}

# ---------------------------------------------------
# parse arguments

while getopts "f:p:h" optchar; do
    case "${optchar}" in
        f) export COMPOSE_FILE=${OPTARG} ;;
        p) export COMPOSE_PROJECT_NAME=${OPTARG} ;;
    esac
done
shift $(expr $OPTIND - 1 )

until
    cmd=$1
    if [ ! -z "$cmd" ]; then
        shift 1
        $cmd "$@"
        if [ $? == 127 ]; then
            help
        fi
        exit
    fi
do
    echo
done

# default behavior
check