""" autopilotpattern/mysql ContainerPilot handlers """
from __future__ import print_function
from datetime import datetime
import os
import socket
import subprocess
import sys

# pylint: disable=invalid-name,no-self-use,dangerous-default-value
from manager.containerpilot import ContainerPilot
from manager.libconsul import Consul
from manager.libmanta import Manta
from manager.libmysql import MySQL, MySQLError
from manager.utils import \
    log, get_ip, debug, \
    UnknownPrimary, WaitTimeoutError, \
    PRIMARY, REPLICA, UNASSIGNED, \
    PRIMARY_KEY, BACKUP_NAME


class Node(object):
    """
    Node represents the state of our running container and carries
    around the MySQL config, and clients for Consul and Manta.
    """
    def __init__(self, mysql=None, cp=None, consul=None, manta=None):
        self.mysql = mysql
        self.consul = consul
        self.manta = manta
        self.cp = cp

        self.hostname = socket.gethostname()
        self.name = 'mysql-{}'.format(self.hostname)
        self.ip = get_ip()

    @debug(log_output=True)
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
            # am I already replicating from somewhere else?
            _, primary_ip = self.mysql.get_primary()
            if not primary_ip:
                pass
            elif primary_ip == self.ip:
                self.cp.state = PRIMARY
                return True
            else:
                self.cp.state = REPLICA
                return False
        except (MySQLError, WaitTimeoutError, UnknownPrimary) as ex:
            log.debug('could not determine primary via mysqld status: %s', ex)

        try:
            # am I already reporting I'm a healthy primary to Consul?
            _, primary_ip = self.consul.get_primary()
            if not primary_ip:
                pass
            elif primary_ip == self.ip:
                self.cp.state = PRIMARY
                return True
            else:
                self.cp.state = REPLICA
                return False
        except (UnknownPrimary, ValueError) as ex:
            log.debug('could not determine primary via Consul: %s', ex)

        self.cp.state = UNASSIGNED
        return False

    def is_replica(self):
        """ check if we're the replica """
        return not self.is_primary() and self.cp.state != UNASSIGNED

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

    # Because we need MySQL up to finish initialization, we need to check
    # for each pass thru the health check that we've done so. The happy
    # path is to check a lock file against the node state (which has been
    # set above) and immediately return when we discover the lock exists.
    # Otherwise, we bootstrap the instance for its *current* state.
    assert_initialized_for_state(node)

    if node.is_primary():
        # If this lock is allowed to expire and the health check for the
        # primary fails the `onChange` handlers for the replicas will try
        # to failover and then the primary will obtain a new lock.
        # If this node can update the lock but the DB fails its health check,
        # then the operator will need to manually intervene if they want to
        # force a failover. This architecture is a result of Consul not
        # permitting us to acquire a new lock on a health-checked session if the
        # health check is *currently* failing, but has the happy side-effect of
        # reducing the risk of flapping on a transient health check failure.
        node.consul.renew_session()

        # Simple health check; exceptions result in a non-zero exit code
        node.mysql.query('select 1')

    elif node.is_replica():
        # TODO: we should make this check actual replication health
        # and not simply that replication has been established
        if not node.mysql.query('show slave status'):
            log.error('Replica is not replicating.')
            sys.exit(1)
    else:
        # If we're still somehow marked UNASSIGNED we exit now. This is a
        # byzantine failure mode where the end-user needs to intervene.
        log.error('Cannot determine MySQL state; failing health check.')
        sys.exit(1)

    node.consul.unlock_failover()


@debug
def on_change(node):
    """ The top-level ContainerPilot onChange handler """

    # first check if this node has already been set primary by a completed
    # call to failover and update the ContainerPilot config as needed.
    if node.is_primary():
        log.debug('[on_change] this node is primary, no failover required.')
        if node.cp.update():
            # we're ignoring the lock here intentionally
            node.consul.put(PRIMARY_KEY, node.hostname)
            node.cp.reload()
        return

    # check if another node has been set primary already and is reporting
    # as healthy, in which case there's no failover required. Note that
    # we can't simply check if we're a replica via .is_replica() b/c that
    # trusts mysqld's view of the world.
    try:
        node.consul.get_primary(timeout=1)
        log.debug('[on_change] primary is already healthy, no failover required')
        return
    except (UnknownPrimary, WaitTimeoutError) as ex:
        log.debug('[on_change] no primary from consul: %s', ex)

    if node.consul.lock_failover(node.name):
        try:
            nodes = node.consul.client.health.service(REPLICA, passing=True)[1]
            ips = [instance['Service']['Address'] for instance in nodes]
            log.info('[on_change] Executing failover with candidates: %s', ips)
            node.mysql.failover(ips)
        except Exception:
            # On failure we bubble-up the exception and fail the onChange.
            # Either another instance that didn't overlap in time will
            # complete failover or we'll be left w/o a primary and require
            # manual intervention via `mysqlrpladmin failover`
            node.consul.unlock_failover()
            raise
    else:
        log.info('[on_change] Failover in progress on another node, '
                 'waiting to complete.')
        node.consul.wait_for_failover_lock()

    # need to determine replicaton status at this point, so make
    # sure we refresh .state from mysqld/Consul
    node.cp.state = UNASSIGNED
    if node.is_primary():
        log.info('[on_change] node %s is primary after failover', node.hostname)
        if node.cp.update():
            # we're intentionally ignoring the advisory lock here
            ok = node.consul.put(PRIMARY_KEY, node.hostname)
            log.debug('[on_change] %s obtained lock: %s', node.hostname, ok)
            node.cp.reload()
        return
    elif node.is_replica():
        log.info('[on_change] node %s is replica after failover', node.hostname)

    if node.cp.state == UNASSIGNED:
        log.error('[on_change] this node is neither primary or replica '
                  'after failover; check replication status on cluster.')
        sys.exit(1)


@debug
def snapshot_task(node):
    """
    Create a snapshot and send it to the object store if this is the
    node and time to do so.
    """
    # bail-out early if we can avoid making a DB connection
    if not node.is_snapshot_node() or not node.consul.lock_snapshot(node.name):
        return

    binlog_file = node.mysql.get_binlog()
    if node.consul.is_snapshot_stale(binlog_file):
        # we'll let exceptions bubble up here. The task will fail
        # and be logged, and when the BACKUP_LOCK_KEY expires we can
        # alert on that externally.
        try:
            write_snapshot(node)
        finally:
            node.consul.unlock_snapshot()


@debug
def write_snapshot(node):
    """
    Calls out to innobackupex to snapshot the DB, then pushes the file
    to Manta and writes that the work is completed in Consul.
    """
    now = datetime.utcnow()
    # we don't want .isoformat() here because of URL encoding
    backup_id = now.strftime('{}'.format(BACKUP_NAME))
    backup_time = now.isoformat()

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
    log.info('snapshot uploaded to %s/%s', node.manta.bucket, backup_id)

    # write the filename of the binlog to Consul so that we know if
    # we've rotated since the last backup.
    # query lets KeyError bubble up -- something's broken
    results = node.mysql.query('show master status')
    binlog_file = results[0]['File']
    node.consul.record_backup(backup_id, backup_time, binlog_file)

# ---------------------------------------------------------
# run_as_* functions determine the top-level behavior of a node

@debug(log_output=True)
def assert_initialized_for_state(node):
    """
    If the node has not yet been set up, find the correct state and
    initialize for that state. After the first health check we'll have
    written a lock file and will never hit this path again.
    """
    LOCK_PATH = '/var/run/init.lock'
    try:
        os.mkdir(LOCK_PATH, 0700)
    except OSError:
        # the lock file exists so we've already initialized
        return True

    # the check for primary will set the state if its known. If another
    # instance is the primary then we'll be marked as REPLICA, so if
    # we can't determine after the check which we are then we're likely
    # the first instance (this will get safely verified later).
    if node.is_primary() or node.cp.state == UNASSIGNED:
        try:
            if not run_as_primary(node):
                log.error('Tried to mark node %s primary but primary exists, '
                          'exiting for retry on next check.', node.name)
                os.rmdir(LOCK_PATH)
                sys.exit(1)
        except MySQLError as ex:
            # We've made it only partly thru setup. Setup isn't idempotent
            # but should be safe to retry if we can make more progress. At
            # worst we end up with a bunch of failure logs.
            log.error('Failed to set up %s as primary (%s). Exiting but will '
                      'retry setup. Check logs following this line to see if '
                      'setup needs reconfiguration or manual intervention to '
                      'continue.', node.name, ex)
            os.rmdir(LOCK_PATH)
            sys.exit(1)
        if node.cp.update():
            node.cp.reload()
            # this is racy with the SIGHUP that ContainerPilot just got
            # sent, but if the Consul agent shuts down quickly enough we
            # end up sending extra API calls to it and get a bunch of log
            # spam. This forces us to exit early.
            sys.exit(0)
    else:
        try:
            run_as_replica(node)
        except (UnknownPrimary, MySQLError) as ex:
            log.error('Failed to set up %s for replication (%s). Exiting for retry '
                      'on next check.', node.name, ex)
            os.rmdir(LOCK_PATH)
            sys.exit(1)
    return False


@debug
def run_as_primary(node):
    """
    The overall workflow here is ported and reworked from the
    Oracle-provided Docker image:
    https://github.com/mysql/mysql-docker/blob/mysql-server/5.7/docker-entrypoint.sh
    """
    if not node.consul.mark_as_primary(node.name):
        return False
    node.cp.state = PRIMARY

    conn = node.mysql.wait_for_connection()
    my = node.mysql
    if conn:
        # if we can make a connection w/o a password then this is the
        # first pass. *Note: the conn is not the same as `node.conn`!*
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
    return True

@debug
def run_as_replica(node):
    """
    Set up GTID-based replication to the primary; once this is set the
    replica will automatically try to catch up with the primary's last
    transactions. UnknownPrimary or mysqlconn.Errors are allowed to
    bubble up to the caller.
    """
    log.info('Setting up replication.')
    node.cp.state = REPLICA
    _, primary_ip = node.consul.get_primary(timeout=30)
    node.mysql.setup_replication(primary_ip)

# ---------------------------------------------------------

def main():
    """
    Parse argument as command and execute that command with
    parameters containing the state of MySQL, ContainerPilot, etc.
    Default behavior is to run `pre_start` DB initialization.
    """
    if len(sys.argv) == 1:
        consul = Consul(envs={'CONSUL': os.environ.get('CONSUL', 'consul')})
        cmd = pre_start
    else:
        consul = Consul()
        try:
            cmd = globals()[sys.argv[1]]
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
