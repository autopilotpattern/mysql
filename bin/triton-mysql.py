from __future__ import print_function
import fcntl
import getpass
import json
import logging
import os
import pwd
import re
import signal
import socket
import struct
import string
import subprocess
import sys
import time

import pymysql
import consul as pyconsul

logging.basicConfig(format='%(asctime)s %(levelname)s %(name)s %(message)s',
                    stream=sys.stdout,
                    level=logging.INFO)
consul = pyconsul.Consul(host=os.environ.get('CONSUL', 'consul'))
config = None

# consts for node state
PRIMARY = 'mysql-primary'
STANDBY = 'mysql-standby'
REPLICA = 'mysql'

# ---------------------------------------------------------
# MySQLNode represents an instance of a mysql container.

class MySQLNode():

    def __init__(self, name='', ip='', state=None):
        self.hostname = socket.gethostname()
        self.name = name if name else 'mysql-{}'.format(self.hostname)
        self.ip = ip if ip else get_ip()
        self.state = state

    # return the node state if we have it, otherwise fetch from Consul
    # and cache the result
    def get_state(self):
        if not self.state:
            if self.hostname == get_primary_node():
                self.state = PRIMARY
            elif self.hostname == get_standby_node():
                self.state = STANDBY
        logging.info('get_state: %s', self.state)
        return self.state

# ---------------------------------------------------------
# MySQLConfig is where we store and access environment variables and render
# the mysqld configuration file based on those values.

class MySQLConfig(object):

    def __init__(self):
        self.mysql_db = os.environ.get('MYSQL_DATABASE', None)
        self.mysql_user = os.environ.get('MYSQL_USER', None)
        self.mysql_password = os.environ.get('MYSQL_PASSWORD', None)
        self.mysql_root_password = os.environ.get('MYSQL_ROOT_PASSWORD', '')
        self.mysql_random_root_password = os.environ.get('MYSQL_RANDOM_ROOT_PASSWORD', True)
        self.mysql_onetime_password = os.environ.get('MYSQL_ONETIME_PASSWORD', False)
        self.repl_user = os.environ.get('MYSQL_REPL_USER', None)
        self.repl_password = os.environ.get('MYSQL_REPL_PASSWORD', None)
        self.datadir = os.environ.get('MYSQL_DATADIR', '/var/lib/mysql')
        self.manta_user = os.environ.get('MANTA_USER', None)
        self.manta_key = os.environ.get('MANTA_KEY_ID', None)
        self.manta_url = os.environ.get('MANTA_URL', 'https://us-east.manta.joyent.com')

        # make sure that if we've pulled in an external data volume that
        # the mysql user can read it
        my_user = pwd.getpwnam('mysql')
        os.chown(self.datadir, my_user.pw_uid, my_user.pw_gid)


    # Write-out config files, even if we've previously initialized the DB,
    # so that we can account for changed hostnames, resized containers, etc.
    def render(self):

        # replace innodb_buffer_pool_size value from environment
        # or use a sensible default (70% of available physical memory)
        innodb_buffer_pool_size = int(os.environ.get('INNODB_BUFFER_POOL_SIZE', 0))
        if not innodb_buffer_pool_size:
            with open('/proc/meminfo', 'r') as memInfoFile:
                memInfo = memInfoFile.read()
                base = re.search('^MemTotal: *(\d+)', memInfo).group(1)
                innodb_buffer_pool_size = int((int(base) / 1024) * 0.7)

        # replace server-id with ID derived from hostname
        # ref https://dev.mysql.com/doc/refman/5.7/en/replication-configuration.html
        hostname=socket.gethostname()
        server_id=int(str(hostname)[:4], 16)

        with open('/etc/my.cnf.tmpl', 'r') as f:
            template = string.Template(f.read())
            rendered = template.substitute(buffer=innodb_buffer_pool_size,
                                           server_id=server_id,
                                           hostname=hostname)
        with open('/etc/my.cnf', 'w') as f:
            f.write(rendered)

        # If provided, create the necessary key files for accessing Manta,
        # based on env vars provided in the docker run command.
        if self.manta_key:
            path = '/{}/.ssh'.format(getpass.getuser())
            os.mkdir(path)
            with open(path + '/manta', 'w') as f:
                f.write(self.manta_key)

# ---------------------------------------------------------
# Containerbuddy config is where we rewrite Containerbuddy's own config
# so that we can dynamically alter what service we advertise

class Containerbuddy(object):

    def __init__(self, node):
        # TODO: we should make sure we can support JSON-in-env-var
        # the same as Containerbuddy itself
        self.node = node
        self.path = os.environ.get('CONTAINERBUDDY').replace('file://', '')
        with open(self.path, 'r') as f:
            self.config = json.loads(f.read())

    def update(self):
        state = self.node.get_state()
        if self.config['services'][0]['name'] != state:
            self.config['services'][0]['name'] = state
            self.render()
            return True

    def render(self):
        new_config = json.dumps(self.config)
        logging.info(new_config)
        with open(self.path, 'w') as f:
            f.write(new_config)

    # force Containerbuddy to reload its configuration
    def reload(self):
        logging.info('Reloading Containerbuddy configuration.')
        #subprocess.call(['kill', '-HUP', '1'])
        os.kill(1, signal.SIGHUP)

# ---------------------------------------------------------
# Top-level functions called by Containerbuddy

# Set up this node as the primary (if none yet exists), or the
# standby (if none yet exists), or replica (default case)
def on_start():
    if not get_primary_node():
        run_as_primary()
    elif not get_standby_node():
        run_as_standby()
    else:
        run_as_replica()

    sys.exit(0)

# run a simple health check
def health():
    try:
        logging.info('health fired!')
        node = MySQLNode()
        cb = Containerbuddy(node)
        if cb.update():
            cb.reload()

        if node.state == STANDBY:
            check_for_snapshot(node)
        # TODO: getting access denied for root@localhost
        # mysql_exec(pymysql.connect(), 'SELECT 1', ())
        sys.exit(0)
    except Exception as ex:
        logging.exception(ex)
        sys.exit(1)

# this will be where we hook-in failover behaviors
def on_change():
    try:
        # TODO
        logging.info('on_change fired!')
        node = MySQLNode()
        cb = Containerbuddy(node)
        if cb.update():
            cb.reload()
        sys.exit(0)
    except Exception as ex:
        logging.exception(ex)
        sys.exit(1)


# ---------------------------------------------------------
# run_as_* functions determine the top-level behavior of a node

# ported and reworked from Oracle-provided Docker image:
# https://github.com/mysql/mysql-docker/blob/mysql-server/5.7/docker-entrypoint.sh
def run_as_primary():
    node = MySQLNode(state=PRIMARY)
    mark_as_primary(node)
    if os.path.isdir(os.path.join(config.datadir, 'mysql')):
        node.temp_pid = start_temp_db()
        node.conn = wait_for_connection()
        stop_replication(node.conn)
    else:
        initialize_db()
        node.temp_pid = start_temp_db()
        set_timezone_info()
        node.conn = wait_for_connection()
        setup_root_user(node.conn)
        create_db(node.conn)
        create_default_user(node.conn)
        create_repl_user(node.conn)
        run_external_scripts('/etc/initdb.d')
        expire_root_password(node.conn)

    cleanup_temp_db(node.temp_pid)

# the startup of a standby is identical to the replica except that
# we mark ourselves as standby first.
def run_as_standby():
    node = MySQLNode(state=STANDBY)
    mark_as_standby(node)
    _run_replica(node)

def run_as_replica():
    node = MySQLNode(state=REPLICA)
    _run_replica(node)

def _run_replica(node):
    initialize_db()
    node.temp_pid = start_temp_db()
    set_timezone_info()
    node.conn = wait_for_connection()

    logging.info('Setting up replication.')
    if has_snapshot(node.conn):
        get_snapshot(node.conn)

    set_primary_for_replica(node.conn)
    cleanup_temp_db(node.temp_pid)


# ---------------------------------------------------------
# bootstrapping functions


# returns the PID of the mysqld process
def start_temp_db():
    pid = subprocess.Popen(['mysqld',
                            '--user=mysql',
                            '--datadir={}'.format(config.datadir),
                            '--skip-networking',
                            '--skip-slave-start']).pid
    logging.info('Running temporary bootstrap mysqld (PID %s)', pid)
    return pid

# clean up the temporary mysqld service that we use for bootstrapping
def cleanup_temp_db(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        logging.info('Failed to close temp DB at PID %s', pid)


def wait_for_connection(timeout=30):
    while timeout > 0:
        try:
            sock = '/var/lib/mysql/mysql.sock'
            return pymysql.connect(unix_socket=sock,
                                   user='root',
                                   charset='utf8',
                                   connect_timeout=timeout)
        except pymysql.err.OperationalError:
            timeout = timeout - 1
            if timeout == 0:
                raise
            time.sleep(1)

def mark_with_cas(key, val, timeout=10):
    while timeout > 0:
        try:
            return consul.kv.put(key, val, cas=0)
        except Exception as ex:
            timeout = timeout - 1
            time.sleep(1)
    raise Exception('Could not reach Consul.')


# ---------------------------------------------------------
# functions to support initialization

# Write flag to Consul to mark this node as primary
def mark_as_primary(node):
    if not mark_with_cas('mysql-primary', node.hostname):
        logging.error('Tried to mark node primary but primary exists, '
                      'restarting bootstrap process.')
        on_start()
    node.state = PRIMARY

def initialize_db():
    try:
        os.mkdir(config.datadir, 0755)
    except OSError:
        pass
    my_user = pwd.getpwnam('mysql')
    os.chown(config.datadir, my_user.pw_uid, my_user.pw_gid)
    logging.info('Initializing database...')
    try:
        subprocess.check_call(['mysqld',
                               '--user=mysql',
                               '--initialize-insecure=on',
                               '--datadir={}'.format(config.datadir)])
    except subprocess.CalledProcessError:
        logging.exception('Failed to initialize database.')
        raise

    logging.info('Database initialized.')


def setup_root_user(conn):
    if config.mysql_random_root_password:
        bytes = os.urandom(128)
        chars = string.ascii_letters + string.digits + '!@#$%&^*()'
        passwd = ''.join([chars[int(os.urandom(1).encode('hex'), 16) % len(chars)]
                          for x in range(20)])
        config.mysql_root_password = passwd
        logging.info('Generated root password: %s', config.mysql_root_password)
    sql = ('SET @@SESSION.SQL_LOG_BIN=0;'
           'DELETE FROM `mysql`.`user` where user != \'mysql.sys\';'
           'CREATE USER `root`@`%%` IDENTIFIED BY %s ;'
           'GRANT ALL ON *.* TO `root`@`%%` WITH GRANT OPTION ;'
           'DROP DATABASE IF EXISTS test ;'
           'FLUSH PRIVILEGES ;')
    mysql_exec(conn, sql, (config.mysql_root_password,))


def expire_root_password(conn):
    if config.mysql_onetime_password:
        mysql_exec(conn, 'ALTER USER `root`@`%%` PASSWORD EXPIRE', ())


def create_db(conn):
    if config.mysql_db:
        sql = 'CREATE DATABASE IF NOT EXISTS `{}`;'.format(config.mysql_db)
        mysql_exec(conn, sql, ())

def create_default_user(conn):
    if not config.mysql_user or not config.mysql_password:
        logging.error('No default user/password configured.')
        return

    # PyMySQL doesn't treat symbols the same as strings when passing
    # parameters, so we need to build the SQL string like this.
    # ref https://github.com/PyMySQL/PyMySQL/issues/271
    sql = 'CREATE USER `{}`@`%%` IDENTIFIED BY %s; '.format(config.mysql_user)
    if config.mysql_db:
        sql += ('GRANT ALL ON `{}`.* TO `{}`@`%%`; '
                .format(config.mysql_db, config.mysql_user))
    sql += 'FLUSH PRIVILEGES;'
    mysql_exec(conn, sql, (config.mysql_password,))


def create_repl_user(conn):
    if not config.repl_user or not config.repl_password:
        logging.error('No replication user/password configured.')
        return
    mysql_exec(conn, ('CREATE USER `{0}`@`%%` IDENTIFIED BY %s; '
                      'GRANT REPLICATION SLAVE ON *.* TO `{0}`@`%%`; '
                      'FLUSH PRIVILEGES;'.format(config.repl_user)),
               (config.repl_password))


# Write timezone data from the node to mysqld by piping the output
# of mysql_tzinfo_to_sql to the mysql client.
# This is kinda gross but the PyMySQL client has a bug where this
# large bulk insert causes a BrokenPipeError exception.
# ref https://github.com/PyMySQL/PyMySQL/issues/397
def set_timezone_info():

    # because we're using the external mysql client, we need to
    # check that the daemon is up but then close the connection
    # so that the socket isn't locked.
    conn = wait_for_connection()
    conn.close()

    pipeline = ('/usr/bin/mysql_tzinfo_to_sql /usr/share/zoneinfo | '
                '/usr/bin/mysql -uroot --protocol=socket '
                '--socket=/var/lib/mysql/mysql.sock')
    try:
        subprocess.check_output(pipeline)
    except (subprocess.CalledProcessError, OSError):
        logging.error('mysql_tzinfo_to_sql returned error.')


# TODO
# run user-defined files added to /etc/initdb.d in a child Docker image
def run_external_scripts(path):
    # for f in /etc/initdb.d/*; do
    # case "$f" in
    #     *.sh)  log "$0: running $f"; . "$f" ;;
    #     *.sql) log "$0: running $f"; "${mysql[@]}" < "$f" && echo ;;
    #     *)     log "$0: ignoring $f" ;;
    #     esac
    # done
    pass


# ---------------------------------------------------------
# functions to support replication

def mark_as_standby(node):
    if not mark_with_cas('mysql-standby', node.hostname):
        logging.error('Tried to mark node standby but standby exists, '
                      'restarting bootstrap process.')
        on_start()
    node.state = STANDBY

def stop_replication(conn):
    mysql_exec(conn, 'STOP SLAVE', ())


# TODO:
# check if there are files on Manta
def has_snapshot(conn):
    logging.info('has_snapshot')
    return True

# TODO
# pull files from Manta
def get_snapshot(conn):
    logging.info('get_snapshot')
    pass

# TODO
# check if it's time to do a snapshot
def check_for_snapshot(node):
    logging.info('check_for_snapshot')
    pass

# TODO
# halt replication and copy files to Manta
def write_snapshot(conn):
    logging.info('write_snapshot')
    pass

# Set up GTID-based replication to the primary; once this is set the
# replica will automatically try to catch up with the primary by pulling
# its entire binlog. This is comparatively slow but suitable for small
# databases.
def set_primary_for_replica(conn):
    primary = get_primary_host()
    sql = ('CHANGE MASTER TO '
           'MASTER_HOST           = %s, '
           'MASTER_USER           = %s, '
           'MASTER_PASSWORD       = %s, '
           'MASTER_PORT           = 3306, '
           'MASTER_CONNECT_RETRY  = 60, '
           'MASTER_AUTO_POSITION  = 1, '
           'MASTER_SSL            = 0; '
           'START SLAVE;')
    logging.info(sql)
    mysql_exec(conn, sql, (primary, config.repl_user, config.repl_password,))


# Query Consul for healthy mysql nodes and check their ServiceID vs
# the primary node. Returns the IP address of the matching node or
# raises an exception.
def get_primary_host(timeout=30):

    primary = get_primary_node()
    logging.info('Checking if primary (%s) is healthy...', primary)

    while timeout > 0:
        try:
            nodes = consul.catalog.service('mysql-primary')[1]
            ips = [service['ServiceAddress'] for service in nodes
                   if service['ServiceID'].endswith(primary)]
            return ips[0]
        except IndexError:
            logging.error('No mysql-primary found.')
            break
        except:
            timeout = timeout - 1
            time.sleep(1)

    raise Exception('Tried replication setup, but primary is '
                    'set and not healthy.')

def get_primary_node(timeout=10):
    while timeout > 0:
        try:
            return get_from_consul('mysql-primary')
        except Exception as ex:
            timeout = timeout - 1
            time.sleep(1)
    raise ex


def get_standby_node(timeout=10):
    while timeout > 0:
        try:
            return get_from_consul('mysql-standby')
        except Exception as ex:
            timeout = timeout - 1
            time.sleep(1)
    raise ex

# Return the Value field for a given Consul key.
# Handles None results safely but lets all other exceptions
# just bubble up.
def get_from_consul(key):
    result = consul.kv.get(key)
    if result[1]:
        return result[1]['Value']
    return None

# ---------------------------------------------------------
# utility functions

def mysql_exec(conn, sql, params):
    try:
        with conn.cursor() as cursor:
            logging.debug(sql)
            logging.debug(params)
            cursor.execute(sql, params)
            conn.commit()
    except:
        raise # re-raise so that we exit


# Use Linux SIOCGIFADDR ioctl to get the IP for the interface.
# ref http://code.activestate.com/recipes/439094-get-the-ip-address-associated-with-a-network-inter/
def get_ip(iface='eth0'):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    return socket.inet_ntoa(fcntl.ioctl(
        s.fileno(),
        0x8915, # SIOCGIFADDR
        struct.pack('256s', iface[:15])
    )[20:24])


# ---------------------------------------------------------

# default behavior will be to start mysqld, running the
# initialization if required
if __name__ == '__main__':
    config = MySQLConfig()
    config.render()
    if len(sys.argv) > 1:
        try:
            locals()[sys.argv[1]]()
        except KeyError:
            logging.error('Invalid command %s', sys.argv[1])
            sys.exit(1)
    else:
        on_start()
