from __future__ import print_function
from datetime import datetime
import fcntl
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
import manta

logging.basicConfig(format='%(asctime)s %(levelname)s %(name)s %(message)s',
                    stream=sys.stdout,
                    level=logging.getLevelName(
                        os.environ.get('LOG_LEVEL', 'DEBUG')))
requests_logger = logging.getLogger('requests')
requests_logger.setLevel(logging.WARN)

# if we log Manta client at debug we'll barf when it tries to log
# the body of binary data
manta_logger = logging.getLogger('manta')
manta_logger.setLevel(logging.INFO)

log = logging.getLogger('triton-mysql')

consul = pyconsul.Consul(host=os.environ.get('CONSUL', 'consul'))
config = None

# consts for node state
PRIMARY = 'mysql-primary'
STANDBY = 'mysql-standby'
REPLICA = 'mysql'

# determines whether we use the primary for snapshots or a separate standby
# replica that doesn't take part in serving queries.
USE_STANDBY = os.environ.get('USE_STANDBY', False)

# consts for keys
PRIMARY_KEY = os.environ.get('PRIMARY_KEY', 'mysql-primary')
STANDBY_KEY = os.environ.get('STANDBY_KEY', 'mysql-standby')
BACKUP_TTL_KEY = os.environ.get('BACKUP_TTL_KEY', 'mysql-backup-run')
LAST_BACKUP_KEY = os.environ.get('LAST_BACKUP_KEY', 'mysql-last-backup')
LAST_BINLOG_KEY = os.environ.get('LAST_BINLOG_KEY', 'mysql-last-binlog')
BACKUP_NAME = os.environ.get('BACKUP_NAME', 'mysql-backup')
BACKUP_TTL = '{}s'.format(os.environ.get('BACKUP_TTL', 86400)) # every 24 hours
SESSION_CACHE_FILE = os.environ.get('SESSION_CACHE_FILE', '/tmp/mysql-session')
SESSION_NAME = os.environ.get('SESSION_NAME', 'mysql-primary-lock')
SESSION_TTL = int(os.environ.get('SESSION_TTL', 60))

# ---------------------------------------------------------

class MySQLNode(object):
    """ MySQLNode represents an instance of a mysql container. """

    def __init__(self, name='', ip='', state=None):
        self.hostname = socket.gethostname()
        self.name = name if name else get_name()
        self.ip = ip if ip else get_ip()
        self.state = state
        self.temp_pid = None
        self.conn = None

    def get_state(self):
        """
        Return the node state if we have it, otherwise fetch
        from Consul and cache the result.
        """
        if not self.state:
            if self.hostname == get_primary_node():
                self.state = PRIMARY
            elif USE_STANDBY and (self.hostname == get_standby_node()):
                self.state = STANDBY
        return self.state

    def is_primary(self):
        return self.state == PRIMARY

    def is_standby(self):
        return self.state == STANDBY

    def is_snapshot_node(self):
        if USE_STANDBY:
             return self.state == STANDBY
        else:
            return self.state == PRIMARY

# ---------------------------------------------------------

class MySQLConfig(object):
    """
    MySQLConfig is where we store and access environment variables and render
    the mysqld configuration file based on those values.
    """

    def __init__(self):
        self.mysql_db = os.environ.get('MYSQL_DATABASE', None)
        self.mysql_user = os.environ.get('MYSQL_USER', None)
        self.mysql_password = os.environ.get('MYSQL_PASSWORD', None)
        self.mysql_root_password = os.environ.get('MYSQL_ROOT_PASSWORD', '')
        self.mysql_random_root_password = self._parse_environ_bool(
            'MYSQL_RANDOM_ROOT_PASSWORD', True)
        self.mysql_onetime_password = self._parse_environ_bool(
            'MYSQL_ONETIME_PASSWORD', False)
        self.repl_user = os.environ.get('MYSQL_REPL_USER', None)
        self.repl_password = os.environ.get('MYSQL_REPL_PASSWORD', None)
        self.datadir = os.environ.get('MYSQL_DATADIR', '/var/lib/mysql')

        # make sure that if we've pulled in an external data volume that
        # the mysql user can read it
        take_ownership(self)

    def _parse_environ_bool(self, var, default=True):
        """
        Parse environment variable strings like "yes/no", "on/off",
        "true/false", "1/0" into a bool.
        """
        val = os.environ.get(var, default)
        try:
            return bool(int(val))
        except ValueError:
            val = val.lower()
            if val in ('false', 'off', 'no'):
                return False
            # non-"1" or "0" string, we'll treat as truthy
            return True


    def render(self):
        """
        Writes-out config files, even if we've previously initialized the DB,
        so that we can account for changed hostnames, resized containers, etc.
        """

        # replace innodb_buffer_pool_size value from environment
        # or use a sensible default (70% of available physical memory)
        innodb_buffer_pool_size = int(os.environ.get('INNODB_BUFFER_POOL_SIZE', 0))
        if not innodb_buffer_pool_size:
            with open('/proc/meminfo', 'r') as memInfoFile:
                memInfo = memInfoFile.read()
                base = re.search(r'^MemTotal: *(\d+)', memInfo).group(1)
                innodb_buffer_pool_size = int((int(base) / 1024) * 0.7)

        # replace server-id with ID derived from hostname
        # ref https://dev.mysql.com/doc/refman/5.7/en/replication-configuration.html
        hostname = socket.gethostname()
        server_id = int(str(hostname)[:4], 16)

        with open('/etc/my.cnf.tmpl', 'r') as f:
            template = string.Template(f.read())
            rendered = template.substitute(buffer=innodb_buffer_pool_size,
                                           server_id=server_id,
                                           hostname=hostname)
        with open('/etc/my.cnf', 'w') as f:
            f.write(rendered)

# ---------------------------------------------------------

class Manta(object):
    """
    The Manta class wraps access to the Manta object store, where we'll put
    our MySQL backups.
    """

    def __init__(self):
        self.account = os.environ.get('MANTA_USER', None)
        self.user = os.environ.get('MANTA_SUBUSER', None)
        self.role = os.environ.get('MANTA_ROLE', None)
        self.key_id = os.environ.get('MANTA_KEY_ID', None)
        self.private_key = os.environ.get('MANTA_PRIVATE_KEY').replace('#', '\n')
        self.url = os.environ.get('MANTA_URL',
                                  'https://us-east.manta.joyent.com')
        self.bucket = os.environ.get('MANTA_BUCKET',
                                     '/{}/stor'.format(self.account))

        self.signer = manta.PrivateKeySigner(self.key_id, self.private_key)
        self.client = manta.MantaClient(self.url,
                                        self.account,
                                        subuser=self.user,
                                        role=self.role,
                                        signer=self.signer)

    def get_backup(self, backup_id, outfile):
        mpath = '{}/{}'.format(self.bucket, backup_id)
        data = self.client.get_object(mpath)
        with open(outfile, 'w') as f:
            f.write(data)

    def put_backup(self, backup_id, infile):
        # TODO: stream this backup once python-manta supports it:
        # ref https://github.com/joyent/python-manta/issues/6
        mpath = '{}/{}'.format(self.bucket, backup_id)
        with open(infile, 'r') as f:
            self.client.put_object(mpath, file=f)

# ---------------------------------------------------------

class ContainerPilot(object):
    """
    ContainerPilot config is where we rewrite ContainerPilot's own config
    so that we can dynamically alter what service we advertise
    """

    def __init__(self, node):
        # TODO: we should make sure we can support JSON-in-env-var
        # the same as ContainerPilot itself
        self.node = node
        self.path = os.environ.get('CONTAINERPILOT').replace('file://', '')
        with open(self.path, 'r') as f:
            self.config = json.loads(f.read())

    def update(self):
        state = self.node.get_state()
        if state and self.config['services'][0]['name'] != state:
            self.config['services'][0]['name'] = state
            self.render()
            return True

    def render(self):
        new_config = json.dumps(self.config)
        log.info(new_config)
        with open(self.path, 'w') as f:
            f.write(new_config)

    def reload(self):
        """ force ContainerPilot to reload its configuration """
        log.info('Reloading ContainerPilot configuration.')
        os.kill(1, signal.SIGHUP)

# ---------------------------------------------------------
# Top-level functions called by ContainerPilot or forked by this program

def on_start():
    """
    Set up this node as the primary (if none yet exists), or the
    standby (if none yet exists), or replica (default case)
    """
    primary = get_primary_node()
    if not primary or primary == get_name():
        run_as_primary()
        return
    elif USE_STANDBY:
        standby = get_standby_node()
        if not standby or standby == get_name():
            run_as_standby()
            return
    run_as_replica()

def health():
    """
    Run a simple health check. Also acts as a check for whether the
    ContainerPilot configuration needs to be reloaded (if it's been
    changed externally), or if we need to make a backup because the
    backup TTL has expired.
    """
    log.debug('health check fired.')
    try:
        node = MySQLNode()
        cp = ContainerPilot(node)
        if cp.update():
            cp.reload()
            return

        # cp.reload() will exit early so no need to setup
        # connection until this point
        ctx = dict(user=config.repl_user,
                   password=config.repl_password,
                   database=config.mysql_db,
                   timeout=cp.config['services'][0]['ttl'])
        node.conn = wait_for_connection(**ctx)

        # Update our lock on being the primary/standby.
        # If this lock is allowed to expire and the health check for the primary
        # fails, the `onChange` handlers for the replicas will try to self-elect
        # as primary by obtaining the lock.
        # If this node can update the lock but the DB fails its health check,
        # then the operator will need to manually intervene if they want to
        # force a failover. This architecture is a result of Consul not
        # permitting us to acquire a new lock on a health-checked session if the
        # health check is *currently* failing, but has the happy side-effect of
        # reducing the risk of flapping on a transient health check failure.
        if node.is_primary() or node.is_standby():
            update_session_ttl()

        if (node.is_snapshot_node() and
                (is_binlog_stale(node.conn) or is_time_for_snapshot())):
            try:
                write_snapshot(node.conn)
            except Exception as ex:
                # we're going to log but not sys.exit(1) here so that
                # we don't mark the primary as unhealthy when a backup
                # fails. The BACKUP_TTL_KEY will expire so we can alert
                # on that externally.
                log.exception(ex)

        mysql_query(node.conn, 'SELECT 1', ())
        sys.exit(0)
    except Exception as ex:
        log.exception(ex)
        sys.exit(1)


def on_change():
    log.debug('on_change check fired.')
    try:
        node = MySQLNode()
        cp = ContainerPilot(node)
        cp.update() # this will populate MySQLNode state correctly
        if node.is_primary():
            return

        ctx = dict(user=config.repl_user,
                   password=config.repl_password,
                   database=config.mysql_db,
                   timeout=cp.config['services'][0]['ttl'])
        node.conn = wait_for_connection(**ctx)

        # need to stop replication whether we're the new primary or not
        stop_replication(node.conn)

    except Exception as ex:
        log.exception(ex)
        sys.exit(1)


    while True:
        try:
            # if there is no primary node, we'll try to obtain the lock.
            # if we get the lock we'll reload as the new primary, otherwise
            # someone else got the lock but we don't know who yet so loop
            primary = get_primary_node()
            if not primary:
                session_id = get_session(no_cache=True)
                if mark_with_session(PRIMARY_KEY, node.hostname, session_id):
                    node.state = PRIMARY
                    if cp.update():
                        cp.reload()
                    return
                else:
                    # we lost the race to lock the session for ourselves
                    log.debug('could not lock session')
                    time.sleep(1)
                    continue

            # we know who the primary is but not whether they're healthy.
            # if it's not healthy, we'll throw an exception and start over.
            ip = get_primary_host(primary=primary)
            if ip == node.ip:
                if cp.update():
                    cp.reload()
                return

            set_primary_for_replica(node.conn)
            return

        except Exception as ex:
            # This exception gets thrown if the session lock for `mysql-primary`
            # key has not expired yet (but there's no healthy primary either),
            # or if the replica's target primary isn't ready yet.
            log.debug(ex)
            time.sleep(1) # avoid hammering Consul
            continue


def create_snapshot():
    log.debug('create_snapshot')

    # we don't want .isoformat() here because of URL encoding
    now = datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')
    backup_id = '{}-{}'.format(BACKUP_NAME, now)

    with open('/tmp/backup.tar', 'w') as f:
        subprocess.check_call(['/usr/bin/innobackupex',
                               '--user={}'.format(config.repl_user),
                               '--password={}'.format(config.repl_password),
                               '--no-timestamp',
                               #'--compress',
                               '--stream=tar',
                               '/tmp/backup'], stdout=f)

    manta_config.put_backup(backup_id, '/tmp/backup.tar')
    consul.kv.put(LAST_BACKUP_KEY, backup_id)

    ctx = dict(user=config.repl_user,
               password=config.repl_password,
               database=config.mysql_db)
    conn = wait_for_connection(**ctx)

    # write the filename of the binlog to Consul so that we know if
    # we've rotated since the last backup.
    # query lets IndexError bubble up -- something's broken
    results = mysql_query(conn, 'SHOW MASTER STATUS', ())
    binlog_file = results[0][0]
    consul.kv.put(LAST_BINLOG_KEY, binlog_file)


# ---------------------------------------------------------
# run_as_* functions determine the top-level behavior of a node

def run_as_primary():
    """
    The overall workflow here is ported and reworked from the
    Oracle-provided Docker image:
    https://github.com/mysql/mysql-docker/blob/mysql-server/5.7/docker-entrypoint.sh
    """
    node = MySQLNode(state=PRIMARY)
    mark_as_primary(node)
    if os.path.isdir(os.path.join(config.datadir, 'mysql')):
        node.temp_pid = start_temp_db(gtid_on=True)
        node.conn = wait_for_connection()
        stop_replication(node.conn) # in case this is a newly-promoted primary
    else:
        if not initialize_db():
            log.info('Skipping database setup.')
            return
        log.info('Continuing database setup.')
        node.temp_pid = start_temp_db(gtid_on=True)
        set_timezone_info()
        node.conn = wait_for_connection()
        setup_root_user(node.conn)
        create_db(node.conn)
        create_default_user(node.conn)
        create_repl_user(node.conn)
        run_external_scripts('/etc/initdb.d')
        expire_root_password(node.conn)

    if USE_STANDBY:
        # if we're using a standby instance then we need to first
        # snapshot the primary so that we can bootstrap the standby.
        write_snapshot(node.conn)

    cleanup_temp_db(node.temp_pid)


def run_as_standby():
    """
    The startup of a standby is identical to the replica except that
    we mark ourselves as standby first.
    """
    node = MySQLNode(state=STANDBY)
    mark_as_standby(node)
    _run_replica(node)

def run_as_replica():
    node = MySQLNode(state=REPLICA)
    _run_replica(node)

def _run_replica(node):
    try:
        log.info('Setting up replication.')
        last_backup = has_snapshot()
        if last_backup:
            get_snapshot(last_backup)
            restore_from_snapshot(last_backup)
        else:
            log.info('Initializing as replica.')
            initialize_db()

        node.temp_pid = start_temp_db(gtid_on=True)
        ctx = dict(user=config.repl_user,
                   password=config.repl_password,
                   database=config.mysql_db)
        node.conn = wait_for_connection(**ctx)
        set_primary_for_replica(node.conn)
    except Exception as ex:
        log.exception(ex)
    finally:
        if node.temp_pid:
            cleanup_temp_db(node.temp_pid)


# ---------------------------------------------------------
# bootstrapping functions


def start_temp_db(gtid_on=False):
    """ Return the PID of the mysqld process """
    args = ['mysqld',
            '--user=mysql',
            '--datadir={}'.format(config.datadir),
            '--skip-networking',
            '--skip-slave-start']
    if gtid_on:
        args.extend(['--gtid-mode=ON',
                     '--enforce-gtid-consistency=ON',
                     '--log-bin=mysql-bin',
                     '--log_slave_updates=ON'])

    pid = subprocess.Popen(args).pid
    log.info('Running temporary bootstrap mysqld (PID %s)', pid)
    return pid

def cleanup_temp_db(pid):
    """
    Clean up the temporary mysqld service that we use for bootstrapping
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        log.warn('Failed to close temp DB at PID %s', pid)


def wait_for_connection(user='root', password=None, database=None, timeout=30):
    while timeout > 0:
        try:
            sock = '/var/run/mysqld/mysqld.sock'
            return pymysql.connect(unix_socket=sock,
                                   user=user,
                                   password=password,
                                   database=database,
                                   charset='utf8',
                                   connect_timeout=timeout)
        except pymysql.err.OperationalError:
            timeout = timeout - 1
            if timeout == 0:
                raise
            time.sleep(1)

def mark_with_session(key, val, session_id, timeout=10):
    log.debug('mark_with_session')
    while timeout > 0:
        try:
            return consul.kv.put(key, val, acquire=session_id)
        except Exception:
            timeout = timeout - 1
            time.sleep(1)
    raise Exception('Could not reach Consul.')

# ---------------------------------------------------------
# functions to support initialization

def mark_as_primary(node):
    """ Write flag to Consul to mark this node as primary """
    session_id = get_session()
    if not mark_with_session(PRIMARY_KEY, node.hostname, session_id):
        log.error('Tried to mark node primary but primary exists, '
                  'restarting bootstrap process.')
        on_start()
    node.state = PRIMARY

def initialize_db():
    """
    post-installation run to set up data directories
    and install mysql.user tables
    """
    make_datadir()
    log.info('Initializing database...')
    try:
        subprocess.check_call(['/usr/bin/mysql_install_db',
                               '--user=mysql',
                               '--datadir={}'.format(config.datadir)])
        log.info('Database initialized.')
        return True
    except subprocess.CalledProcessError:
        log.warn('Database was previously initialized.')
        return False

def make_datadir():
    try:
        os.mkdir(config.datadir, 0770)
    except OSError:
        pass
    take_ownership(config)


def take_ownership(config, owner='mysql'):
    """
    Set ownership of all directories and files under config.datadir
    to `owner`'s UID and GID. Defaults to setting ownership for
    mysql user.
    """
    directory = config.datadir
    user = pwd.getpwnam(owner)
    os.chown(directory, user.pw_uid, user.pw_gid)
    for root, dirs, files in os.walk(config.datadir):
        for dir in dirs:
            os.chown(os.path.join(root, dir), user.pw_uid, user.pw_gid)
        for f in files:
            os.chown(os.path.join(root, f), user.pw_uid, user.pw_gid)


def setup_root_user(conn):
    if config.mysql_random_root_password:
        # we could use --random-passwords in our call to `mysql_install_db`
        # instead here but we want to have the root password available
        # until we're done with this setup.
        chars = string.ascii_letters + string.digits + '!@#$%&^*()'
        passwd = ''.join([chars[int(os.urandom(1).encode('hex'), 16) % len(chars)]
                          for _ in range(20)])
        config.mysql_root_password = passwd
        log.info('Generated root password: %s', config.mysql_root_password)
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
        log.error('No default user/password configured.')
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
    """ this user will be used for both replication and backups """
    if not config.repl_user or not config.repl_password:
        log.error('No replication user/password configured.')
        return
    mysql_exec(
        conn,
        ('CREATE USER `{0}`@`%%` IDENTIFIED BY %s; '
         'GRANT SUPER, REPLICATION SLAVE, RELOAD, LOCK TABLES, REPLICATION CLIENT '
         'ON *.* TO `{0}`@`%%`; '
         'FLUSH PRIVILEGES;'.format(config.repl_user)),
        (config.repl_password))


def set_timezone_info():
    """
    Write timezone data from the node to mysqld by piping the output
    of mysql_tzinfo_to_sql to the mysql client.
    This is kinda gross but the PyMySQL client has a bug where this
    large bulk insert causes a BrokenPipeError exception.
    ref https://github.com/PyMySQL/PyMySQL/issues/397
    """

    conn = wait_for_connection()
    # because we're using the external mysql client, we need to
    # check that the daemon is up but then close the connection
    # so that the socket isn't locked.
    conn.close()

    pipeline = ('/usr/bin/mysql_tzinfo_to_sql /usr/share/zoneinfo | '
                '/usr/bin/mysql -uroot --protocol=socket '
                '--socket=/var/lib/mysql/mysql.sock')
    try:
        subprocess.check_output(pipeline)
    except (subprocess.CalledProcessError, OSError):
        log.error('mysql_tzinfo_to_sql returned error.')


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
    session_id = get_session()
    if not mark_with_session(STANDBY_KEY, node.hostname, session_id):
        log.error('Tried to mark node standby but standby exists, '
                  'restarting bootstrap process.')
        on_start()
    node.state = STANDBY

def stop_replication(conn):
    mysql_exec(conn, 'STOP SLAVE', ())


def get_session(no_cache=False):
    """
    Gets a Consul session ID from the on-disk cache or calls into
    `create_session` to generate and cache a new one.
    """
    if no_cache:
        return create_session()
    try:
        with open(SESSION_CACHE_FILE, 'r') as f:
            session_id = f.read()
    except IOError:
        session_id = create_session()
    return session_id

def create_session(ttl=SESSION_TTL):
    """
    We can't rely on storing Consul session IDs in memory because
    `health` and `onChange` handler calls happen in a subsequent
    process. Here we creates a session on Consul and cache the
    session ID to disk. Returns the session ID.
    """
    session_id = consul.session.create(name=SESSION_NAME,
                                       behavior='release',
                                       ttl=ttl)
    with open(SESSION_CACHE_FILE, 'w') as f:
        f.write(session_id)
    return session_id

def update_session_ttl(session_id=None):
    """ Renews the session TTL on Consul """
    if not session_id:
        session_id = get_session()
    consul.session.renew(session_id)


def has_snapshot():
    """ Ask Consul for 'last backup' key """
    log.debug('has_snapshot')
    last_backup_id = get_from_consul(LAST_BACKUP_KEY)
    return last_backup_id

def get_snapshot(filename):
    """
    Pull files from Manta; let exceptions bubble up to the caller
    """
    log.debug('get_snapshot')
    try:
        os.mkdir('/tmp/backup', 0770)
    except OSError:
        pass
    outfile = '/tmp/backup/{}'.format(filename)
    manta_config.get_backup(filename, outfile)

def restore_from_snapshot(filename):
    make_datadir()
    infile = '/tmp/backup/{}'.format(filename)
    subprocess.check_call(['tar', '-xif', infile, '-C', '/tmp/backup'])
    subprocess.check_call(['/usr/bin/innobackupex',
                           '--force-non-empty-directories',
                           '--copy-back',
                           '/tmp/backup'])
    take_ownership(config)

def is_binlog_stale(conn):
    results = mysql_query(conn, 'SHOW MASTER STATUS', ())
    try:
        binlog_file = results[0][0]
        last_binlog_file = get_from_consul(LAST_BINLOG_KEY)
    except IndexError:
        return True
    return binlog_file != last_binlog_file

def is_time_for_snapshot():
    """ Check if it's time to do a snapshot """
    log.debug('is_time_for_snapshot')
    try:
        check = consul.agent.checks()[BACKUP_TTL_KEY]
        log.debug(check)
        if check['Status'] == 'passing':
            return False
        return True
    except KeyError:
        return True

def write_snapshot(conn):
    """
    Create a new snapshot, upload it to Manta, and register it
    with Consul. Exceptions bubble up to caller
    """
    log.debug('write_snapshot')


    # we set the BACKUP_TTL before we run the backup so that we don't
    # have multiple health checks running concurrently. We then fork the
    # create_snapshot call and return. The snapshot process will be
    # re-parented to ContainerPilot
    set_backup_ttl()
    subprocess.Popen(['python', '/usr/local/bin/triton-mysql.py', 'create_snapshot'])

def set_backup_ttl():
    """
    Write a TTL check for the BACKUP_TTL key.
    Exceptions are allowed to bubble up to the caller
    """
    log.debug('set_backup_ttl')
    log.debug(BACKUP_TTL_KEY)
    try:
        pass_check = consul.agent.check.ttl_pass(BACKUP_TTL_KEY)
        log.debug(pass_check)
        if not pass_check:
            log.debug('no pass_check!')
            consul.agent.check.register(name=BACKUP_TTL_KEY,
                                        check=pyconsul.Check.ttl(BACKUP_TTL),
                                        check_id=BACKUP_TTL_KEY)
            pass_check = consul.agent.check.ttl_pass(BACKUP_TTL_KEY)
            log.debug(pass_check)
            if not pass_check:
                raise Exception('Could not register health check for {}'
                                .format(BACKUP_TTL_KEY))
    except Exception as ex:
        log.exception(ex)

    return

def set_primary_for_replica(conn):
    """
    Set up GTID-based replication to the primary; once this is set the
    replica will automatically try to catch up with the primary's last
    transactions.
    """
    log.debug('set_primary_for_replica')
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
    mysql_exec(conn, sql, (primary, config.repl_user, config.repl_password,))


def get_primary_host(primary=None, timeout=30):
    """
    Query Consul for healthy mysql nodes and check their ServiceID vs
    the primary node. Returns the IP address of the matching node or
    raises an exception.
    """
    log.debug('get_primary_host')
    if not primary:
        primary = get_primary_node()
    if not primary:
        raise Exception('Tried replication setup but could not find primary.')
    log.debug('Checking if primary (%s) is healthy...', primary)

    while timeout > 0:
        try:
            nodes = consul.health.service(PRIMARY_KEY, passing=True)[1]
            ips = [service['Service']['Address'] for service in nodes
                   if service['Service']['ID'].endswith(primary)]
            return ips[0]
        except Exception:
            timeout = timeout - 1
            time.sleep(1)

    raise Exception('Tried replication setup, but primary is '
                    'set and not healthy.')

def get_primary_node(timeout=10):
    log.debug('get_primary_node')
    while timeout > 0:
        try:
            result = consul.kv.get(PRIMARY_KEY)
            if result[1]:
                if result[1].get('Session', False):
                    return result[1]['Value']
            # either there is no primary or the session has expired
            return None
        except Exception as ex:
            timeout = timeout - 1
            time.sleep(1)
    raise ex


def get_standby_node(timeout=10):
    while timeout > 0:
        try:
            result = consul.kv.get(STANDBY_KEY)
            if result[1]:
                if result[1].get('Session', False):
                    return result[1]['Value']
            # either there is no standby or the session has expired
            return None
        except Exception as ex:
            timeout = timeout - 1
            time.sleep(1)
    raise ex

def get_from_consul(key):
    """
    Return the Value field for a given Consul key.
    Handles None results safely but lets all other exceptions
    just bubble up.
    """
    result = consul.kv.get(key)
    if result[1]:
        return result[1]['Value']
    return None

# ---------------------------------------------------------
# utility functions

def mysql_exec(conn, sql, params):
    try:
        with conn.cursor() as cursor:
            log.debug(sql)
            log.debug(params)
            cursor.execute(sql, params)
            conn.commit()
    except Exception:
        raise # re-raise so that we exit

def mysql_query(conn, sql, params):
    try:
        with conn.cursor() as cursor:
            log.debug(sql)
            log.debug(params)
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Exception:
        raise # re-raise so that we exit


def get_ip(iface='eth0'):
    """
    Use Linux SIOCGIFADDR ioctl to get the IP for the interface.
    ref http://code.activestate.com/recipes/439094-get-the-ip-address-associated-with-a-network-inter/
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    return socket.inet_ntoa(fcntl.ioctl(
        sock.fileno(),
        0x8915, # SIOCGIFADDR
        struct.pack('256s', iface[:15])
    )[20:24])

def get_name():
    return 'mysql-{}'.format(socket.gethostname())

# ---------------------------------------------------------

if __name__ == '__main__':

    config = MySQLConfig()
    config.render()
    manta_config = Manta()

    if len(sys.argv) > 1:
        try:
            locals()[sys.argv[1]]()
        except KeyError:
            log.error('Invalid command %s', sys.argv[1])
            sys.exit(1)
    else:
        # default behavior will be to start mysqld, running the
        # initialization if required
        on_start()
