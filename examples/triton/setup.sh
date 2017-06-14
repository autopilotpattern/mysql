#!/bin/bash
set -e -o pipefail

help() {
    echo
    echo 'Usage ./setup.sh ~/path/to/MANTA_PRIVATE_KEY'
    echo
    echo 'Checks that your Triton and Docker environment is sane and configures'
    echo 'an environment file to use.'
    echo
    echo 'MANTA_PRIVATE_KEY is the filesystem path to an SSH private key'
    echo 'used to connect to Manta for the database backups.'
    echo
    echo 'Additional details must be configured in the _env file, but this script will properly'
    echo 'encode the SSH key details for use with this MySQL image.'
    echo
}


# populated by `check` function whenever we're using Triton
TRITON_USER=
TRITON_DC=

# ---------------------------------------------------
# Top-level commands

# Check for correct configuration and setup _env file
envcheck() {

    if [ -z "$1" ]; then
        tput rev  # reverse
        tput bold # bold
        echo 'Please provide a path to a SSH private key to access Manta.'
        tput sgr0 # clear

        help
        exit 1
    fi

    if [ ! -f "$1" ]; then
        tput rev  # reverse
        tput bold # bold
        echo 'SSH private key for Manta is unreadable.'
        tput sgr0 # clear

        help
        exit 1
    fi

    # Assign args to named vars
    MANTA_PRIVATE_KEY_PATH=$1

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
    if [ ! -f "_env" ]; then
        echo '# Environment variables for MySQL service' > _env
        echo 'MYSQL_USER=dbuser' >> _env
        echo 'MYSQL_PASSWORD='$(cat /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 7) >> _env
        echo 'MYSQL_REPL_USER=repluser' >> _env
        echo 'MYSQL_REPL_PASSWORD='$(cat /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 7) >> _env
        echo 'MYSQL_DATABASE=demodb' >> _env
        echo >> _env

        echo '# Environment variables for backups to Manta' >> _env
        echo 'MANTA_URL=https://us-east.manta.joyent.com' >> _env
        echo 'MANTA_BUCKET= # an existing Manta bucket' >> _env
        echo 'MANTA_USER= # a user with access to that bucket' >> _env
        echo 'MANTA_SUBUSER=' >> _env
        echo 'MANTA_ROLE=' >> _env

        # MANTA_KEY_ID must be the md5 formatted key fingerprint. A SHA256 will result in errors.
        set +o pipefail
        # The -E option was added to ssh-keygen recently; if it doesn't work, then
        # assume we're using an older version of ssh-keygen that only outputs MD5 fingerprints
        ssh-keygen -yl -E md5 -f ${MANTA_PRIVATE_KEY_PATH} > /dev/null 2>&1
        if [ $? -eq 0 ]; then
            echo MANTA_KEY_ID=$(ssh-keygen -yl -E md5 -f ${MANTA_PRIVATE_KEY_PATH} | awk '{print substr($2,5)}') >> _env
        else
            echo MANTA_KEY_ID=$(ssh-keygen -yl -f ${MANTA_PRIVATE_KEY_PATH} | awk '{print $2}') >> _env
        fi
        set -o pipefail

        # munge the private key so that we can pass it into an env var sanely
        # and then unmunge it in our startup script
        echo MANTA_PRIVATE_KEY=$(cat ${MANTA_PRIVATE_KEY_PATH} | tr '\n' '#') >> _env
        echo >> _env

        echo 'Edit the _env file with your desired MYSQL_* and MANTA_* config'
    else
        echo 'Existing _env file found, exiting'
        exit
    fi
}

get_root_password() {
    echo $(docker logs ${COMPOSE_PROJECT_NAME:-mysql}_mysql_1 2>&1 | \
               awk '/Generated root password/{print $NF}' | \
               awk '{$1=$1};1'
        ) | pbcopy
}



# ---------------------------------------------------
# parse arguments

# Get function list
funcs=($(declare -F -p | cut -d " " -f 3))

until
    if [ ! -z "$1" ]; then
        # check if the first arg is a function in this file, or use a default
        if [[ " ${funcs[@]} " =~ " $1 " ]]; then
            cmd=$1
            shift 1
        else
            cmd="envcheck"
        fi

        $cmd "$@"
        if [ $? == 127 ]; then
            help
        fi

        exit
    else
        help
    fi
do
    echo
done
