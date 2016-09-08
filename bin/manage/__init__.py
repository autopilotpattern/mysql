""" autopilotpattern/mysql ContainerPilot handlers """
from __future__ import print_function
from datetime import datetime
import fcntl
import os
import socket
import subprocess
import sys
import time

# pylint: disable=invalid-name,no-self-use,dangerous-default-value
from manage.containerpilot import ContainerPilot
from manage.libconsul import Consul, get_primary_host, \
    mark_as_primary
from manage.libmanta import Manta
from manage.libmysql import MySQL, MySQLError
from manage.utils import \
    log, get_ip, debug, \
    UnknownPrimary, \
    PRIMARY, REPLICA, UNASSIGNED, \
    PRIMARY_KEY, BACKUP_TTL_KEY, LAST_BINLOG_KEY, BACKUP_TTL, \
    BACKUP_NAME, LAST_BACKUP_KEY


class Node(object):
    """
    Node represents the state of our running container and carries
    around the MySQL config, and clients for Consul and Manta.
    """
    def __init__(self, mysql=None, cp=None, consul=None, manta=None,
                 name='', ip=''):
        self.mysql = mysql
        self.consul = consul
        self.manta = manta
        self.cp = cp

        # these fields can all be overriden for dependency injection
        # in testing only; don't pass these args in normal operation
        self.hostname = name if name else socket.gethostname()
        self.name = name if name else 'mysql-{}'.format(self.hostname)
        self.ip = ip if ip else get_ip()

    def is_primary(self):
        """
        Check if this node is the primary by checking in-memory cache,
        then Consul, then MySQL replication status. Caches its result so
        the node `state` field needs to be set to UNASSIGNED if you want
        to force a check of Consul, etc.
        """
        if self.cp.state != UNASSIGNED:
            return self.cp.state == PRIMARY

        try:
            # am I already reporting I'm a healthy primary to Consul?
            primary_node, _ = self.consul.get_primary()
            if primary_node == self.name:
                return True
        except UnknownPrimary:
            pass

        try:
            # am I already replicating from somewhere else?
            repl_master = self.mysql.get_repl_status()
            if not repl_master or repl_master == self.ip:
                return True
        except MySQLError:
            pass

        return False

    def is_replica(self):
        """ check if we're the replica """
        return not self.is_primary()

    def is_snapshot_node(self):
        """ check if we're the node that's going to execute the snapshot """
        # TODO: we want to have the replicas all do a lock on the snapshot task
        return self.is_primary()


# ---------------------------------------------------------
# Top-level functions called by ContainerPilot

@debug
def pre_start(node):
    """
    the top-level ContainerPilot `preStart` handler.
    MySQL must be running in order to execute most of our setup behavior
    so we're just going to make sure the directory structures are in
    place and then let the first health check handler take it from there
    """
    # make sure that if we've pulled in an external data volume that
    # the mysql user can read it
    my = node.mysql
    my.take_ownership()
    my.render()
    if not os.path.isdir(os.path.join(my.datadir, 'mysql')):
        last_backup = node.consul.has_snapshot()
        if last_backup:
            node.manta.get_backup(last_backup)
            my.restore_from_snapshot(last_backup)
        else:
            if not my.initialize_db():
                log.info('Skipping database setup.')

@debug
def health(node):
    """
    The top-level ContainerPilot `health` handler. Runs a simple health check.
    Also acts as a check for whether the ContainerPilot configuration needs
    to be reloaded (if it's been changed externally).
    """
    try:
        if node.cp.update():
            node.cp.reload()
            return

        # Because we need MySQL up to finish initialization, we need to check
        # for each pass thru the health check that we've done so. The happy
        # path is to check a lock file against the node state (which has been
        # set above) and immediately return when we discover the lock exists.
        # Otherwise, we bootstrap the instance for its *current* state.
        assert_initialized_for_state(node)

        # Update our lock on being the primary
        # If this lock is allowed to expire and the health check for the primary
        # fails, the `onChange` handlers for the replicas will try to self-elect
        # as primary by obtaining the lock.
        # If this node can update the lock but the DB fails its health check,
        # then the operator will need to manually intervene if they want to
        # force a failover. This architecture is a result of Consul not
        # permitting us to acquire a new lock on a health-checked session if the
        # health check is *currently* failing, but has the happy side-effect of
        # reducing the risk of flapping on a transient health check failure.
        if node.is_primary():
            node.consul.renew_session()

        node.mysql.query(node.mysql.conn, 'SELECT 1', ())
    except Exception as ex:
        log.exception(ex)
        sys.exit(1)


@debug
def on_change(node):
    """ The top-level ContainerPilot onChange handler """

    @debug(log_output=True)
    def am_i_primary():
        """ Try to figure out if this is the primary now """
        results = node.mysql.query(node.mysql.conn, 'SHOW SLAVE STATUS', ())
        log.debug('[on_change DEBUG] %s', results)
        if results:
            master_ip = results[0][1]
            log.debug('[on_change] master is %s and my IP is %s', master_ip, node.ip)
            if master_ip != node.ip:
                return False
        return False

    @debug
    def set_me_primary(node):
        """ Set this node as the primary """
        node.state = PRIMARY
        node.cp.state = PRIMARY
        if node.cp.update():
            # TODO: double-check this?
            # we're ignoring the lock here intentionally
            node.consul.put(PRIMARY_KEY, node.hostname)
            node.cp.reload()

    # check if there's a healthy primary first otherwise this
    # is an infinite loop! we only continue if there is an
    # exception when we try to get the hostname
    if node.is_primary():
        log.debug('[on_change] this node is primary, no failover required.')
        return
    try:
        if get_primary_host(node.consul, timeout=1):
            log.debug('[on_change] primary is already healthy, no failover required')
            return
    except UnknownPrimary:
        pass
    if am_i_primary():
        set_me_primary(node.consul)
        return

    try:
        log.debug('[on_change DEBUG] entering failover')

        _key = 'FAILOVER_IN_PROGRESS'
        log.debug('[on_change DEBUG] _key=%s', _key)

        session_id = node.consul.create_session(_key, ttl=120)
        log.debug('[on_change DEBUG] session_id=%s', session_id)
        ok = node.consul.lock(_key, node.hostname, session_id)
        log.debug('[on_change DEBUG] consul.kv.put -> %s', ok)
        if not ok:
            log.info('[on_change] Waiting for failover to complete')
            while True:
                if node.consul.is_locked(_key):
                    time.sleep(3)
        else:
            log.debug('[on_change] setting up failover')
            nodes = node.consul.health.service(REPLICA, passing=True)[1]
            ips = [instance['Service']['Address'] for instance in nodes]
            repl_user = node.mysql.repl_user
            repl_password = node.mysql.repl_password
            candidates = ','.join(["{}:'{}'@{}".format(repl_user, repl_password, ip)
                                   for ip in ips])
            log.info('[on_change] Executing failover with candidates: %s', ips)
            subprocess.check_call(
                ['mysqlrpladmin',
                 '--slaves={}'.format(candidates),
                 '--candidates={}'.format(candidates),
                 '--rpl-user={}:{}'.format(repl_user, repl_password),
                 'failover']
            )

        log.debug('[on_change] failover complete, figuring out my slave status: %s', node.ip)
        if am_i_primary():
            set_me_primary(node.consul)
            return
        else:
            log.debug('[on_change] this node is a replica')

    except Exception as ex:
        log.exception(ex)
        sys.exit(1)
    finally:
        try:
            log.debug('[on_change] unlocking session')
            node.consul.unlock(_key, session_id)
        except Exception:
            log.debug('[on_change] could not unlock session')


@debug
def snapshot_task(node):
    """
    Create a snapshot and send it to the object store if this is the
    node and time to do so.
    """

    @debug
    def is_backup_running():
        """ Run only one snapshot on an instance at a time """
        # TODO: I think we can just write here and bail?
        lockfile_name = '/tmp/{}'.format(BACKUP_TTL_KEY)
        try:
            backup_lock = open(lockfile_name, 'r+')
        except IOError:
            backup_lock = open(lockfile_name, 'w')
        try:
            fcntl.flock(backup_lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(backup_lock, fcntl.LOCK_UN)
            return False
        except IOError:
            return True
        finally:
            backup_lock.close()

    @debug
    def is_binlog_stale(node):
        """ Compare current binlog to that recorded w/ Consul """
        results = node.mysql.query('SHOW MASTER STATUS')
        try:
            binlog_file = results[0][0]
            last_binlog_file = node.consul.get(LAST_BINLOG_KEY)
        except IndexError:
            return True
        return binlog_file != last_binlog_file

    @debug
    def is_time_for_snapshot(consul):
        """ Check if it's time to do a snapshot """
        if consul.is_check_healthy(BACKUP_TTL_KEY):
            return False
        return True

    if not node.is_snapshot_node() or is_backup_running():
        # bail-out early if we can avoid making a DB connection
        return

    if is_binlog_stale(node) or is_time_for_snapshot(node.consul):
        # we'll let exceptions bubble up here. The task will fail
        # and be logged, and when the BACKUP_TTL_KEY expires we can
        # alert on that externally.
        write_snapshot(node)

@debug
def write_snapshot(node):
    """
    Create a new snapshot, upload it to Manta, and register it
    with Consul. Exceptions bubble up to caller
    """
    # we set the BACKUP_TTL before we run the backup so that we don't
    # have multiple health checks running concurrently.
    set_backup_ttl(node)
    create_snapshot(node)

# TODO: break this up into smaller functions inside the write_snapshot scope
@debug(log_output=True)
def create_snapshot(node):
    """
    Calls out to innobackupex to snapshot the DB, then pushes the file
    to Manta and writes that the work is completed in Consul.
    """

    try:
        lockfile_name = '/tmp/{}'.format(BACKUP_TTL_KEY)
        backup_lock = open(lockfile_name, 'r+')
    except IOError:
        backup_lock = open(lockfile_name, 'w')

    try:
        fcntl.flock(backup_lock, fcntl.LOCK_EX|fcntl.LOCK_NB)

        # we don't want .isoformat() here because of URL encoding
        backup_id = datetime.utcnow().strftime('{}'.format(BACKUP_NAME))

        with open('/tmp/backup.tar', 'w') as f:
            subprocess.check_call(['/usr/bin/innobackupex',
                                   '--user={}'.format(node.mysql.repl_user),
                                   '--password={}'.format(node.mysql.repl_password),
                                   '--no-timestamp',
                                   #'--compress',
                                   '--stream=tar',
                                   '/tmp/backup'], stdout=f)
        log.info('snapshot completed, uploading to object store')
        node.manta.put_backup(backup_id, '/tmp/backup.tar')

        log.debug('snapshot uploaded to %s/%s, setting LAST_BACKUP_KEY'
                  ' in Consul', node.manta.bucket, backup_id)
        node.consul.put(LAST_BACKUP_KEY, backup_id)

        # write the filename of the binlog to Consul so that we know if
        # we've rotated since the last backup.
        # query lets IndexError bubble up -- something's broken
        results = node.mysql.query('SHOW MASTER STATUS')
        binlog_file = results[0][0]
        log.debug('setting LAST_BINLOG_KEY in Consul')
        node.consul.put(LAST_BINLOG_KEY, binlog_file)

    except IOError:
        return False
    finally:
        log.debug('unlocking backup lock')
        fcntl.flock(backup_lock, fcntl.LOCK_UN)
        log.debug('closing backup file')
        backup_lock.close()


@debug
def set_backup_ttl(node):
    """
    Write a TTL check for the BACKUP_TTL key.
    """
    if not node.consul.pass_check(BACKUP_TTL_KEY):
        node.consul.register_check(BACKUP_TTL_KEY, BACKUP_TTL)
        if not node.consul.pass_check(BACKUP_TTL_KEY):
            log.error('Could not register health check for %s', BACKUP_TTL_KEY)
            return False
    return True



# ---------------------------------------------------------
# run_as_* functions determine the top-level behavior of a node

@debug(log_output=True)
def assert_initialized_for_state(node):
    """
    If the node has not yet been set up, find the correct state and initialize
    for that state. After the first health check we'll have written
    a lock file and will never hit this path again.
    """
    LOCK_PATH = '/var/run/init.lock'
    try:
        os.mkdir(LOCK_PATH, 0700)
    except OSError:
        # the lock file exists so we've already initialized for this state
        return True

    state = node.get_state()
    if not state:
        if not node.is_primary():
            # primary hasn't yet been set in Consul
            state = PRIMARY
        else:
            state = REPLICA
    node.state = state

    if state == PRIMARY:
        run_as_primary(node)
    else:
        run_as_replica(node)
    return False


@debug
def run_as_primary(node):
    """
    The overall workflow here is ported and reworked from the
    Oracle-provided Docker image:
    https://github.com/mysql/mysql-docker/blob/mysql-server/5.7/docker-entrypoint.sh
    """
    node.state = PRIMARY
    mark_as_primary(node)

    conn = node.mysql.wait_for_connection()
    my = node.mysql
    if conn:
        # if we can make a connection w/o a password then this is the
        # first pass. The conn is not the same as `node.conn`!
        my.set_timezone_info()
        my.setup_root_user(conn)
        my.create_db(conn)
        my.create_default_user(conn)
        my.create_repl_user(conn)
        my.expire_root_password(conn)
    else:
        # in case this is a newly-promoted primary
        my.execute('STOP SLAVE')

    # although backups will be run from any instance, we need to first
    # snapshot the primary so that we can bootstrap replicas.
    write_snapshot(node)

@debug
def run_as_replica(node):
    """
    Set up GTID-based replication to the primary; once this is set the
    replica will automatically try to catch up with the primary's last
    transactions. UnknownPrimary or mysqlconn.Errors are re-raised to bubble
    up to the caller.
    """
    log.info('Setting up replication.')
    try:
        primary = get_primary_host(node.consul)
    except UnknownPrimary:
        raise
    node.mysql.add('CHANGE MASTER TO '
                   'MASTER_HOST           = %s, '
                   'MASTER_USER           = %s, '
                   'MASTER_PASSWORD       = %s, '
                   'MASTER_PORT           = 3306, '
                   'MASTER_CONNECT_RETRY  = 60, '
                   'MASTER_AUTO_POSITION  = 1, '
                   'MASTER_SSL            = 0; ',
                   (primary, node.mysql.repl_user, node.mysql.repl_password))
    node.mysql.add('START SLAVE;', ())
    node.mysql.execute_many()

# ---------------------------------------------------------

def main():
    """
    Parse argument as command and execute that command with
    parameters containing the state of MySQL, ContainerPilot, etc.
    Default behavior is to run `pre_start` DB initialization.
    """
    if len(sys.argv) == 0:
        consul = Consul(envs={'CONSUL': os.environ.get('CONSUL', 'consul')})
        cmd = pre_start
    else:
        consul = Consul()
        try:
            cmd = locals()[sys.argv[1]]
        except KeyError:
            log.error('Invalid command: %s', sys.argv[1])
            sys.exit(1)

    my = MySQL()
    manta = Manta()
    cp = ContainerPilot()
    cp.load()
    node = Node(mysql=my, consul=consul, manta=manta, cp=cp)

    cmd(node)

if __name__ == '__main__':
    main()
