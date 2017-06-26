from datetime import datetime, timedelta
import fcntl
import logging
import os
import tempfile
import unittest

# pylint: disable=import-error
import consul as pyconsul
import json5
import mock

import manage
# pylint: disable=invalid-name,no-self-use,dangerous-default-value
from manager.client import MySQL
from manager.config import ContainerPilot
from manager.discovery import Consul
from manager.env import *
from manager.network import *
from manager.storage.manta_stor import Manta
from manager.utils import *


class TestPreStart(unittest.TestCase):

    def setUp(self):
        logging.getLogger().setLevel(logging.WARN)
        consul = mock.MagicMock()
        manta = mock.MagicMock()
        my = mock.MagicMock()
        my.datadir = tempfile.mkdtemp()
        self.node = manage.Node(consul=consul, snaps=manta, mysql=my)

    def tearDown(self):
        logging.getLogger().setLevel(logging.DEBUG)

    def test_pre_start_first_node(self):
        """
        The first node will not attempt to download a snapshot from Manta.
        """
        self.node.consul.has_snapshot.return_value = False
        manage.pre_start(self.node)
        self.node.consul.has_snapshot.assert_called_once()
        self.node.mysql.initialize_db.assert_called_once()
        self.assertFalse(self.node.snaps.get_backup.called)
        self.assertFalse(self.node.mysql.restore_from_snapshot.called)

    def test_pre_start_snapshot_complete(self):
        """
        Given a successful snapshot by the first node, a new node will
        download the snapshot from Manta
        """
        self.node.consul.has_snapshot.return_value = True
        manage.pre_start(self.node)
        self.node.consul.has_snapshot.assert_called_once()
        self.node.snaps.get_backup.assert_called_once()
        self.node.mysql.restore_from_snapshot.assert_called_once()
        self.assertFalse(self.node.mysql.initialize_db.called)

    def test_pre_start_no_reinitialization(self):
        """
        Given a node that's restarted, pre_start should not try
        to re-initialize the node.
        """
        os.mkdir(os.path.join(self.node.mysql.datadir, 'mysql'))
        self.node.consul.has_snapshot.return_value = True
        manage.pre_start(self.node)
        self.assertFalse(self.node.consul.has_snapshot.called)

    def test_pre_start_snapshot_incomplete(self):
        """
        Given a snapshot that has been marked successful but not
        completed, a new node will wait and not crash.
        """
        self.node.consul = Consul(get_environ())
        self.node.consul.client = mock.MagicMock()

        def kv_gets(*args, **kwargs):
            yield pyconsul.ConsulException()
            yield [0, {'Value': '{"id": "xxxx", "dt": "yyyyy"}'}]

        self.node.consul.client.kv.get.side_effect = kv_gets()

        manage.pre_start(self.node)
        self.node.snaps.get_backup.assert_called_once()
        self.assertEqual(self.node.consul.client.kv.get.call_count, 2)
        self.node.mysql.restore_from_snapshot.assert_called_once()
        self.assertFalse(self.node.mysql.initialize_db.called)


class TestHealth(unittest.TestCase):

    LOCK_PATH = '/var/run/init.lock'

    def setUp(self):
        logging.getLogger().setLevel(logging.WARN)
        consul = mock.MagicMock()
        my = mock.MagicMock()
        cp = ContainerPilot()
        cp.load(get_environ())
        temp_file = tempfile.NamedTemporaryFile()
        cp.path = temp_file.name
        my.datadir = tempfile.mkdtemp()
        self.node = manage.Node(consul=consul, cp=cp, mysql=my)
        self.node.ip = '192.168.1.101'
        self.node.name = 'node1'

    def tearDown(self):
        logging.getLogger().setLevel(logging.DEBUG)
        try:
            os.rmdir(self.LOCK_PATH)
        except:
            pass

    def test_primary_first_pass(self):
        """
        Given uninitialized node w/ no other instances running,
        set up for running as the primary.
        """
        self.node.mysql.wait_for_connection.return_value = True
        self.node.mysql.get_primary.side_effect = UnknownPrimary()

        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.mark_as_primary = mock.MagicMock(return_value=True)
        self.node.consul.renew_session = mock.MagicMock()
        manage.write_snapshot = mock.MagicMock(return_value=True)
        self.node.consul.client.health.service.return_value = ()

        try:
            manage.health(self.node)
            self.fail('Should have exited but did not.')
        except SystemExit:
            pass

        calls = [
            mock.call.setup_root_user(True),
            mock.call.create_db(True),
            mock.call.create_default_user(True),
            mock.call.create_repl_user(True),
            mock.call.expire_root_password(True)
        ]
        self.node.mysql.assert_has_calls(calls)
        manage.write_snapshot.assert_called_once()
        self.assertEqual(self.node.cp.state, PRIMARY)

    def test_primary_typical(self):
        """ Typical health check for primary with established replication """
        os.mkdir(self.LOCK_PATH, 0700)
        self.node.mysql.get_primary.return_value = ('node1', '192.168.1.101')
        manage.health(self.node)
        self.node.consul.renew_session.assert_called_once()
        self.node.mysql.query.assert_called_once() # just the select 1
        self.assertEqual(self.node.cp.state, PRIMARY)

    def test_primary_no_replicas(self):
        """ Health check if previously initialized but with no replicas """
        os.mkdir(self.LOCK_PATH, 0700)
        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(return_value=())

        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.renew_session = mock.MagicMock()
        self.node.consul.client.health.service.return_value = [0, [{
            'Service' : {'ID': 'node1', 'Address': '192.168.1.101'},
            }]]

        manage.health(self.node)
        calls = [
            mock.call.query('show slave status'),
            mock.call.query('show slave hosts'),
            mock.call.query('select 1')
        ]
        self.node.mysql.query.assert_has_calls(calls)
        self.node.consul.client.health.service.assert_called_once()
        self.node.consul.renew_session.assert_called_once()
        self.assertEqual(self.node.cp.state, PRIMARY)

    def test_primary_no_replicas_no_consul_state_fails(self):
        """
        Health check if previously initialized but with no replicas
        and no Consul state so we'll remain marked UNASSIGNED which
        needs to be a failing health check.
        """
        os.mkdir(self.LOCK_PATH, 0700)
        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(return_value=())

        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.renew_session = mock.MagicMock()
        self.node.consul.client.health.service.return_value = []

        try:
            logging.getLogger().setLevel(logging.CRITICAL) # noisy
            manage.health(self.node)
            self.fail('Should have exited but did not.')
        except SystemExit:
            pass
        calls = [
            mock.call.query('show slave status'),
            mock.call.query('show slave hosts'),
        ]
        self.node.mysql.query.assert_has_calls(calls)
        self.assertEqual(self.node.consul.client.health.service.call_count, 2)
        self.assertEqual(self.node.cp.state, UNASSIGNED)

    def test_replica_typical(self):
        """
        Typical health check for replica with established replication
        """
        os.mkdir(self.LOCK_PATH, 0700)
        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(return_value=[
            {'Master_Server_Id': 'node2', 'Master_Host': '192.168.1.102'}])

        manage.health(self.node)
        self.assertFalse(self.node.consul.renew_session.called)
        calls = [
            mock.call.query('show slave status'),
            mock.call.query('show slave status')
        ]
        self.node.mysql.query.assert_has_calls(calls)
        self.assertEqual(self.node.cp.state, REPLICA)

    def test_replica_no_replication(self):
        """
        Health check for failure mode where initial replication setup
        failed but a primary already exists in Consul.
        """
        os.mkdir(self.LOCK_PATH, 0700)
        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(return_value=())
        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.renew_session = mock.MagicMock()
        self.node.consul.client.health.service.return_value = [0, [{
            'Service' : {'ID': 'node2', 'Address': '192.168.1.102'},
            }]]

        try:
            logging.getLogger().setLevel(logging.CRITICAL) # noisy
            manage.health(self.node)
            self.fail('Should have exited but did not.')
        except SystemExit:
            pass
        calls = [
            mock.call.query('show slave status'),
            mock.call.query('show slave hosts'),
            mock.call.query('show slave status')
        ]
        self.node.mysql.query.assert_has_calls(calls)
        self.assertFalse(self.node.consul.renew_session.called)
        self.assertEqual(self.node.cp.state, REPLICA)

    def test_replica_first_pass(self):
        """
        Given uninitialized node w/ a health primary, set up replication.
        """
        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock()

        def query_results(*args, **kwargs):
            yield ()
            yield () # and after two hits we've set up replication
            yield [{'Master_Server_Id': 'node2', 'Master_Host': '192.168.1.102'}]

        self.node.mysql.query.side_effect = query_results()
        self.node.mysql.wait_for_connection = mock.MagicMock(return_value=True)
        self.node.mysql.setup_replication = mock.MagicMock(return_value=True)

        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.client.health.service.return_value = [0, [{
            'Service' : {'ID': 'node2', 'Address': '192.168.1.102'},
            }]]

        manage.health(self.node)
        calls = [
            mock.call.query('show slave status'),
            mock.call.query('show slave hosts'),
            mock.call.query('show slave status')
        ]
        self.node.mysql.query.assert_has_calls(calls)
        self.assertEqual(self.node.consul.client.health.service.call_count, 2)
        manage.write_snapshot.assert_called_once()
        self.assertEqual(self.node.cp.state, REPLICA)

    def test_replica_first_pass_replication_setup_fails(self):
        """
        Given uninitialized node w/ failed replication setup, fail
        """
        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(return_value=())
        self.node.mysql.wait_for_connection = mock.MagicMock(return_value=True)
        self.node.mysql.setup_replication = mock.MagicMock(return_value=True)

        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.client.health.service.return_value = [0, [{
            'Service' : {'ID': 'node2', 'Address': '192.168.1.102'},
            }]]
        try:
            logging.getLogger().setLevel(logging.CRITICAL) # noisy
            manage.health(self.node)
            self.fail('Should have exited but did not.')
        except SystemExit:
            pass
        calls = [
            mock.call.query('show slave status'),
            mock.call.query('show slave hosts'),
            mock.call.query('show slave status')
        ]
        self.node.mysql.query.assert_has_calls(calls)
        self.assertEqual(self.node.consul.client.health.service.call_count, 2)
        manage.write_snapshot.assert_called_once()
        self.assertEqual(self.node.cp.state, REPLICA)

    def test_replica_first_pass_primary_lockout(self):
        """
        Given uninitialized node w/ no primary, then a health primary
        retry setting up as a replica
        """
        self.node.mysql.wait_for_connection.return_value = True
        self.node.mysql.get_primary.side_effect = UnknownPrimary()

        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.mark_as_primary = mock.MagicMock(return_value=False)
        self.node.consul.client.health.service.return_value = ()
        try:
            logging.getLogger().setLevel(logging.CRITICAL) # noisy
            manage.health(self.node)
            self.fail('Should have exited but did not.')
        except SystemExit:
            pass

        self.assertEqual(self.node.cp.state, UNASSIGNED)



class TestOnChange(unittest.TestCase):

    def setUp(self):
        logging.getLogger().setLevel(logging.WARN)
        consul = mock.MagicMock()
        my = mock.MagicMock()
        cp = ContainerPilot()
        cp.load(get_environ())
        temp_file = tempfile.NamedTemporaryFile()
        cp.path = temp_file.name
        cp.reload = mock.MagicMock(return_value=True)

        self.node = manage.Node(consul=consul, cp=cp, mysql=my)
        self.node.ip = '192.168.1.101'
        self.node.name = 'node1'

    def tearDown(self):
        logging.getLogger().setLevel(logging.DEBUG)

    def test_this_node_already_set_primary(self):
        """
        Given that another node has run the failover and set this node
        as primary, then this node will be primary and updates its
        ContainerPilot config as required
        """
        self.node.mysql.get_primary.return_value = ('node1', '192.168.1.101')
        manage.on_change(self.node)

        self.node.consul.put.assert_called_once()
        self.node.cp.reload.assert_called_once()
        self.assertEqual(self.node.cp.state, PRIMARY)

    def test_another_node_already_set_primary(self):
        """
        Given that another node has run the failover and set some other
        node as primary, then this node will not be primary and needs to
        do nothing.
        """
        self.node.mysql.get_primary.return_value = ('node1', '192.168.1.102')
        manage.on_change(self.node)

        self.assertFalse(self.node.consul.put.called)
        self.node.consul.get_primary.assert_called_once()
        self.assertFalse(self.node.cp.reload.called)
        self.assertEqual(self.node.cp.state, REPLICA)

    def test_failover_runs_this_node_is_primary(self):
        """
        Given a successful failover where this node is marked primary,
        the node will update its ContainerPilot config as required
        """
        def query_results(*args, **kwargs):
            yield ()
            yield () # and after two hits we've set up replication
            yield [{'Master_Server_Id': 'node1', 'Master_Host': '192.168.1.101'}]

        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(side_effect=query_results())
        self.node.mysql.failover = mock.MagicMock()

        def consul_get_primary_results(*args, **kwargs):
            yield UnknownPrimary()
            yield UnknownPrimary()
            yield ('node1', '192.168.1.101')

        self.node.consul.get_primary.side_effect = consul_get_primary_results()
        self.node.consul.lock.return_value = True
        self.node.consul.read_lock.return_value = None, None
        self.node.consul.client.health.service.return_value = [0, [
            {'Service' : {'ID': 'node1', 'Address': '192.168.1.101'}},
            {'Service' : {'ID': 'node3', 'Address': '192.168.1.103'}}
        ]]

        manage.on_change(self.node)

        self.assertEqual(self.node.consul.get_primary.call_count, 2)
        self.node.consul.lock_failover.assert_called_once()
        self.node.consul.client.health.service.assert_called_once()
        self.assertFalse(self.node.consul.unlock_failover.called)
        self.node.consul.put.assert_called_once()
        self.node.cp.reload.assert_called_once()
        self.assertEqual(self.node.cp.state, PRIMARY)

    def test_failover_runs_another_node_is_primary(self):
        """
        Given a successful failover where another node is marked primary,
        the node will not update its ContainerPilot config
        """
        def query_results(*args, **kwargs):
            yield ()
            yield () # and after two hits we've set up replication
            yield [{'Master_Server_Id': 'node1', 'Master_Host': '192.168.1.102'}]

        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(side_effect=query_results())
        self.node.mysql.failover = mock.MagicMock()

        def consul_get_primary_results(*args, **kwargs):
            yield UnknownPrimary()
            yield UnknownPrimary()
            yield ('node1', '192.168.1.102')

        self.node.consul.get_primary.side_effect = consul_get_primary_results()
        self.node.consul.lock_failover.return_value = True
        self.node.consul.read_lock.return_value = None, None
        self.node.consul.client.health.service.return_value = [0, [
            {'Service' : {'ID': 'node1', 'Address': '192.168.1.101'}},
            {'Service' : {'ID': 'node3', 'Address': '192.168.1.102'}}
        ]]

        manage.on_change(self.node)

        self.assertEqual(self.node.consul.get_primary.call_count, 2)
        self.node.consul.lock_failover.assert_called_once()
        self.node.consul.client.health.service.assert_called_once()
        self.assertFalse(self.node.consul.unlock_failover.called)
        self.assertFalse(self.node.cp.reload.called)
        self.assertEqual(self.node.cp.state, REPLICA)

    def test_failover_fails(self):
        """
        Given a failed failover, ensure we unlock the failover lock
        but exit with an unhandled exception without trying to set
        status.
        """
        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(return_value=())
        self.node.mysql.failover = mock.MagicMock(side_effect=Exception('fail'))

        self.node.consul.get_primary.side_effect = UnknownPrimary()
        self.node.consul.lock_failover.return_value = True
        self.node.consul.read_lock.return_value = None, None
        self.node.consul.client.health.service.return_value = [0, [
            {'Service' : {'ID': 'node1', 'Address': '192.168.1.101'}},
            {'Service' : {'ID': 'node3', 'Address': '192.168.1.102'}}
        ]]

        try:
            manage.on_change(self.node)
            self.fail('Expected unhandled exception but did not.')
        except Exception as ex:
            self.assertEqual(ex.message, 'fail')

        self.assertEqual(self.node.consul.get_primary.call_count, 2)
        self.node.consul.lock_failover.assert_called_once()
        self.node.consul.client.health.service.assert_called_once()
        self.node.consul.unlock_failover.assert_called_once()
        self.assertFalse(self.node.cp.reload.called)
        self.assertEqual(self.node.cp.state, UNASSIGNED)


    def test_failover_locked_this_node_is_primary(self):
        """
        Given another node is running a failover, wait for that failover.
        Given this this node is marked primary, the node will update its
        ContainerPilot config as required.
        """
        def query_results(*args, **kwargs):
            yield ()
            yield () # and after two hits we've set up replication
            yield [{'Master_Server_Id': 'node1', 'Master_Host': '192.168.1.101'}]

        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(side_effect=query_results())
        self.node.mysql.failover = mock.MagicMock()

        def consul_get_primary_results(*args, **kwargs):
            yield UnknownPrimary()
            yield UnknownPrimary()
            yield ('node1', '192.168.1.101')

        def lock_sequence(*args, **kwargs):
            yield True
            yield False

        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.put = mock.MagicMock()
        self.node.consul.get_primary = mock.MagicMock(
            side_effect=consul_get_primary_results())
        self.node.consul.lock_failover = mock.MagicMock(return_value=False)
        self.node.consul.unlock_failover = mock.MagicMock()
        self.node.consul.is_locked = mock.MagicMock(side_effect=lock_sequence())

        with mock.patch('time.sleep'): # cuts 3 sec from test run
            manage.on_change(self.node)

        self.assertEqual(self.node.consul.get_primary.call_count, 2)
        self.node.consul.lock_failover.assert_called_once()
        self.assertFalse(self.node.consul.client.health.service.called)
        self.assertFalse(self.node.consul.unlock_failover.called)
        self.node.consul.put.assert_called_once()
        self.node.cp.reload.assert_called_once()
        self.assertEqual(self.node.cp.state, PRIMARY)


    def test_failover_locked_another_node_is_primary(self):
        """
        Given another node is running a failover, wait for that failover.
        Given this this node is not marked primary, the node will not
        update its ContainerPilot config.
        """
        def query_results(*args, **kwargs):
            yield ()
            yield () # and after two hits we've set up replication
            yield [{'Master_Server_Id': 'node2', 'Master_Host': '192.168.1.102'}]

        self.node.mysql = MySQL(envs=get_environ())
        self.node.mysql._conn = mock.MagicMock()
        self.node.mysql.query = mock.MagicMock(side_effect=query_results())
        self.node.mysql.failover = mock.MagicMock()

        def consul_get_primary_results(*args, **kwargs):
            yield UnknownPrimary()
            yield UnknownPrimary()
            yield ('node2', '192.168.1.102')

        def lock_sequence(*args, **kwargs):
            yield True
            yield False

        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.put = mock.MagicMock()
        self.node.consul.get_primary = mock.MagicMock(
            side_effect=consul_get_primary_results())
        self.node.consul.lock_failover = mock.MagicMock(return_value=False)
        self.node.consul.unlock_failover = mock.MagicMock()
        self.node.consul.is_locked = mock.MagicMock(side_effect=lock_sequence())

        with mock.patch('time.sleep'): # cuts 3 sec from test run
            manage.on_change(self.node)

        self.assertEqual(self.node.consul.get_primary.call_count, 2)
        self.node.consul.lock_failover.assert_called_once()
        self.assertFalse(self.node.consul.client.health.service.called)
        self.assertFalse(self.node.consul.unlock_failover.called)
        self.assertFalse(self.node.consul.put.called)
        self.assertFalse(self.node.cp.reload.called)
        self.assertEqual(self.node.cp.state, REPLICA)


class TestSnapshotTask(unittest.TestCase):

    def setUp(self):
        logging.getLogger().setLevel(logging.WARN)
        consul = mock.MagicMock()
        manta = mock.MagicMock()
        my = mock.MagicMock()
        cp = ContainerPilot()
        cp.load(get_environ())
        my.datadir = tempfile.mkdtemp()
        cp.state = PRIMARY
        my.datadir = tempfile.mkdtemp()
        self.node = manage.Node(consul=consul, cp=cp, snaps=manta, mysql=my)

    def tearDown(self):
        logging.getLogger().setLevel(logging.DEBUG)
        try:
            os.remove('/tmp/mysql-backup-run')
        except OSError:
            pass

    def test_not_snapshot_node(self):
        """ Don't snapshot if this isn't the snapshot node """
        # TODO update when this logic changes
        self.node.cp.state = REPLICA
        manage.snapshot_task(self.node)
        self.assertFalse(self.node.mysql.query.called)

    def test_binlog_stale(self):
        """ Snapshot if the binlog is stale even if its not time to do so """
        consul = Consul(envs=get_environ())
        binlog_file = 'mysql.002'
        now = datetime.utcnow().isoformat()
        consul_values = {
            LAST_BACKUP_KEY: '{{"id": "xxxx", "dt": "{}"}}'.format(now),
            LAST_BINLOG_KEY: 'mysql.001',
        }
        consul.get = consul_values.__getitem__
        self.assertTrue(consul.is_snapshot_stale(binlog_file))

    def test_is_snapshot_stale_invalid(self):
        """ Snapshot if the timer has elapsed even if the binlog isn't stale"""
        consul = Consul(envs=get_environ())
        binlog_file = 'mysql.001'

        consul_values = {
            LAST_BACKUP_KEY: '{"id": "xxxx", "dt": "yyyyy"}',
            LAST_BINLOG_KEY: 'mysql.001',
        }
        consul.get = consul_values.__getitem__
        try:
            self.assertTrue(consul.is_snapshot_stale(binlog_file))
            self.fail('Expected ValueError with invalid data in Consul')
        except ValueError:
            pass

        # not stale
        now = datetime.utcnow().isoformat()
        consul_values = {
            LAST_BACKUP_KEY: '{{"id": "xxxx", "dt": "{}"}}'.format(now),
            LAST_BINLOG_KEY: 'mysql.001',
        }
        consul.get = consul_values.__getitem__
        self.assertFalse(consul.is_snapshot_stale(binlog_file))

        # stale
        then = (datetime.utcnow() - timedelta(hours=25)).isoformat()
        consul_values = {
            LAST_BACKUP_KEY: '{{"id": "xxxx", "dt": "{}"}}'.format(then),
            LAST_BINLOG_KEY: 'mysql.001',
        }
        consul.get = consul_values.__getitem__
        self.assertTrue(consul.is_snapshot_stale(binlog_file))

    def test_backup_already_running(self):
        """ Don't snapshot if there's already a snapshot running """
        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.client.session.create.return_value = 'xyzzy'

        with mock.patch('manage.write_snapshot') as ws:
            lockfile_name = '/tmp/' + BACKUP_LOCK_KEY
            try:
                backup_lock = open(lockfile_name, 'w')
                fcntl.flock(backup_lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
                manage.snapshot_task(self.node)
            finally:
                fcntl.flock(backup_lock, fcntl.LOCK_UN)
                backup_lock.close()
            self.assertFalse(ws.called)

    def test_backup_unlocked(self):
        """
        Make sure that if a snapshot has run that we unlock correctly.
        """
        self.node.consul = Consul(envs=get_environ())
        self.node.consul.client = mock.MagicMock()
        self.node.consul.client.session.create.return_value = 'xyzzy'
        with mock.patch('manage.write_snapshot') as ws:
            lockfile_name = '/tmp/' + BACKUP_LOCK_KEY
            try:
                backup_lock = open(lockfile_name, 'w')
                fcntl.flock(backup_lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
                manage.snapshot_task(self.node)
            finally:
                fcntl.flock(backup_lock, fcntl.LOCK_UN)
                backup_lock.close()
            manage.snapshot_task(self.node)
            self.assertTrue(ws.called)


class TestMySQL(unittest.TestCase):

    def setUp(self):
        logging.getLogger().setLevel(logging.WARN)
        self.environ = get_environ()
        self.my = MySQL(self.environ)
        self.my._conn = mock.MagicMock()

    def tearDown(self):
        logging.getLogger().setLevel(logging.DEBUG)

    def test_parse(self):
        self.assertEqual(self.my.mysql_db, 'test_mydb')
        self.assertEqual(self.my.mysql_user, 'test_me')
        self.assertEqual(self.my.mysql_password, 'test_pass')
        self.assertEqual(self.my.mysql_root_password, 'test_root_pass')
        self.assertEqual(self.my.mysql_random_root_password, True)
        self.assertEqual(self.my.mysql_onetime_password, True)
        self.assertEqual(self.my.repl_user, 'test_repl_user')
        self.assertEqual(self.my.repl_password, 'test_repl_pass')
        self.assertEqual(self.my.datadir, '/var/lib/mysql')
        self.assertEqual(self.my.pool_size, 100)
        self.assertIsNotNone(self.my.ip)

    def test_query_buffer_execute_should_flush(self):
        self.my.add('query 1', ())
        self.assertEqual(len(self.my._query_buffer.items()), 1)
        self.assertEqual(len(self.my._conn.mock_calls), 0)
        self.my.execute('query 2', ())
        self.assertEqual(len(self.my._query_buffer.items()), 0)
        exec_calls = [
            mock.call.cursor().execute('query 1', params=()),
            mock.call.commit(),
            mock.call.cursor().fetchall(),
            mock.call.cursor().execute('query 2', params=()),
            mock.call.commit(),
            mock.call.cursor().fetchall(),
            mock.call.cursor().close()
        ]
        self.assertEqual(self.my._conn.mock_calls[2:], exec_calls)

    def test_query_buffer_execute_many_should_flush(self):
        self.my.add('query 3', ())
        self.my.add('query 4', ())
        self.my.add('query 5', ())
        self.my.execute_many()
        self.assertEqual(len(self.my._query_buffer.items()), 0)
        exec_many_calls = [
            mock.call.cursor().execute('query 3', params=()),
            mock.call.commit(),
            mock.call.cursor().fetchall(),
            mock.call.cursor().execute('query 4', params=()),
            mock.call.commit(),
            mock.call.cursor().fetchall(),
            mock.call.cursor().execute('query 5', params=()),
            mock.call.commit(),
            mock.call.cursor().fetchall(),
            mock.call.cursor().close()
        ]
        self.assertEqual(self.my._conn.mock_calls[2:], exec_many_calls)

    def test_query_buffer_query_should_flush(self):
        self.my.query('query 6', ())
        self.assertEqual(len(self.my._query_buffer.items()), 0)
        query_calls = [
            mock.call.cursor().execute('query 6', params=()),
            mock.call.cursor().fetchall(),
            mock.call.cursor().close()
        ]
        self.assertEqual(self.my._conn.mock_calls[2:], query_calls)

    def test_expected_setup_statements(self):
        conn = mock.MagicMock()
        self.my.setup_root_user(conn)
        self.my.create_db(conn)
        self.my.create_default_user(conn)
        self.my.create_repl_user(conn)
        self.my.expire_root_password(conn)
        self.assertEqual(len(self.my._conn.mock_calls), 0) # use param, not attr
        statements = [args[0] for (name, args, _)
                      in conn.mock_calls if name == 'cursor().execute']
        expected = [
            'SET @@SESSION.SQL_LOG_BIN=0;',
            "DELETE FROM `mysql`.`user` where user != 'mysql.sys';",
            'CREATE USER `root`@`%` IDENTIFIED BY %s ;',
            'GRANT ALL ON *.* TO `root`@`%` WITH GRANT OPTION ;',
            'DROP DATABASE IF EXISTS test ;',
            'FLUSH PRIVILEGES ;',
            'CREATE DATABASE IF NOT EXISTS `test_mydb`;',
            'CREATE USER `test_me`@`%` IDENTIFIED BY %s;',
            'GRANT ALL ON `test_mydb`.* TO `test_me`@`%`;',
            'FLUSH PRIVILEGES;',
            'CREATE USER `test_repl_user`@`%` IDENTIFIED BY %s; ',
            ('GRANT SUPER, SELECT, INSERT, REPLICATION SLAVE, RELOAD,'
             ' LOCK TABLES, GRANT OPTION, REPLICATION CLIENT, RELOAD,'
             ' DROP, CREATE ON *.* TO `test_repl_user`@`%`; '),
            'FLUSH PRIVILEGES;',
            'ALTER USER `root`@`%` PASSWORD EXPIRE']
        self.assertEqual(statements, expected)


class TestConsul(unittest.TestCase):

    def setUp(self):
        self.environ = get_environ()

    def test_parse_with_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '1'
        consul = Consul(self.environ)
        self.assertEqual(consul.host, 'localhost')

    def test_parse_without_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '0'
        consul = Consul(self.environ)
        self.assertEqual(consul.host, 'my.consul.example.com')

        self.environ['CONSUL_AGENT'] = ''
        consul = Consul(self.environ)
        self.assertEqual(consul.host, 'my.consul.example.com')



class TestContainerPilotConfig(unittest.TestCase):

    def setUp(self):
        logging.getLogger().setLevel(logging.WARN)
        self.environ = get_environ()

    def tearDown(self):
        logging.getLogger().setLevel(logging.DEBUG)

    def test_parse_with_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '1'
        cp = ContainerPilot()
        cp.load(envs=self.environ)

        self.assertEqual(cp.config['consul'], 'localhost:8500')
        health_check_exec = cp.config['jobs'][4]['health']['exec']
        self.assertIn('my.consul.example.com', health_check_exec)
        self.assertEqual(cp.state, UNASSIGNED)

    def test_parse_without_consul_agent(self):
        self.environ['CONSUL_AGENT'] = ''
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        self.assertEqual(cp.config['consul'], 'my.consul.example.com:8500')
        self.assertFalse('consul-agent' in
                         [job['name'] for job in cp.config['jobs']])
        self.assertEqual(cp.state, UNASSIGNED)

    def test_update(self):
        self.environ['CONSUL_AGENT'] = '1'
        cp = ContainerPilot()
        cp.state = REPLICA
        cp.load(envs=self.environ)
        temp_file = tempfile.NamedTemporaryFile()
        cp.path = temp_file.name

        # no update expected
        cp.update()
        with open(temp_file.name, 'r') as updated:
            self.assertEqual(updated.read(), '')

        # force an update
        cp.state = PRIMARY
        cp.update()
        with open(temp_file.name, 'r') as updated:
            config = json5.loads(updated.read())
            self.assertEqual(config['consul'], 'localhost:8500')
            health_check_exec = config['jobs'][4]['health']['exec']
            self.assertIn('my.consul.example.com', health_check_exec)


class TestMantaConfig(unittest.TestCase):

    def setUp(self):
        self.environ = get_environ()

    def test_parse(self):
        manta = Manta(self.environ)
        self.assertEqual(manta.account, 'test_manta_account')
        self.assertEqual(manta.user, 'test_manta_subuser')
        self.assertEqual(manta.role, 'test_manta_role')
        self.assertEqual(manta.bucket, '/test_manta_account/stor')
        self.assertEqual(manta.url, 'https://us-east.manta.joyent.com')
        self.assertEqual(
            manta.private_key,
            ('-----BEGIN RSA PRIVATE KEY-----\n'
             'MIIEowIBAAKCAQEAvvljJQt2V3jJoM1SC9FiaBaw5AjVR40v5wKCVaONSz+FWm\n'
             'pc91hUJHQClaxXDlf1p5kf3Oqu5qjM6w8oD7uPkzj++qPnCkzt+JGPfUBxpzul\n'
             '80J0GLHpqQ2YUBXfJ6pCb0g7z/hkdsSwJt7DS+keWCtWpVYswj2Ln8CwNlZlye\n'
             'qAmNE2ePZg8AzfpFmDROljU3GHhKaAviiLyxOklbwSbySbTmdNLHHxu22+ciW9\n'
             '-----END RSA PRIVATE KEY-----'))
        self.assertEqual(manta.key_id,
                         '49:d5:1f:09:5e:46:92:14:c0:46:8e:48:33:75:10:bc')


class TestUtilsEnvironment(unittest.TestCase):

    def test_to_flag(self):
        self.assertEqual(to_flag('yes'), True)
        self.assertEqual(to_flag('Y'), True)
        self.assertEqual(to_flag('no'), False)
        self.assertEqual(to_flag('N'), False)
        self.assertEqual(to_flag('1'), True)
        self.assertEqual(to_flag('xxxxx'), True)
        self.assertEqual(to_flag('0'), False)
        self.assertEqual(to_flag('xxxxx'), True)
        self.assertEqual(to_flag(1), True)
        self.assertEqual(to_flag(0), False)

    def test_env_parse(self):

        os.environ['TestUtilsEnvironment'] = 'PASS'
        environ = {
            'A': '$TestUtilsEnvironment',
            'B': 'PASS  ',
            'C': 'PASS # SOME COMMENT'
        }
        self.assertEqual(env('A', '', environ), 'PASS')
        self.assertEqual(env('B', '', environ), 'PASS')
        self.assertEqual(env('C', '', environ), 'PASS')
        self.assertEqual(env('D', 'PASS', environ), 'PASS')


TEST_ENVIRON = {
    'CONSUL': 'my.consul.example.com',
    'CONSUL_AGENT': '1',

    'CONTAINERPILOT': '/etc/containerpilot.json5',

    'MYSQL_DATABASE': 'test_mydb',
    'MYSQL_USER': 'test_me',
    'MYSQL_PASSWORD': 'test_pass',
    'MYSQL_ROOT_PASSWORD': 'test_root_pass',
    'MYSQL_RANDOM_ROOT_PASSWORD': 'Y',
    'MYSQL_ONETIME_PASSWORD': '1',
    'MYSQL_REPL_USER': 'test_repl_user',
    'MYSQL_REPL_PASSWORD': 'test_repl_pass',
    'INNODB_BUFFER_POOL_SIZE': '100',

    'MANTA_USER': 'test_manta_account',
    'MANTA_SUBUSER': 'test_manta_subuser',
    'MANTA_ROLE': 'test_manta_role',
    'MANTA_KEY_ID': '49:d5:1f:09:5e:46:92:14:c0:46:8e:48:33:75:10:bc',
    'MANTA_PRIVATE_KEY': (
        '-----BEGIN RSA PRIVATE KEY-----#'
        'MIIEowIBAAKCAQEAvvljJQt2V3jJoM1SC9FiaBaw5AjVR40v5wKCVaONSz+FWm#'
        'pc91hUJHQClaxXDlf1p5kf3Oqu5qjM6w8oD7uPkzj++qPnCkzt+JGPfUBxpzul#'
        '80J0GLHpqQ2YUBXfJ6pCb0g7z/hkdsSwJt7DS+keWCtWpVYswj2Ln8CwNlZlye#'
        'qAmNE2ePZg8AzfpFmDROljU3GHhKaAviiLyxOklbwSbySbTmdNLHHxu22+ciW9#'
        '-----END RSA PRIVATE KEY-----')
}

def get_environ():
    return TEST_ENVIRON.copy()


if __name__ == '__main__':
    unittest.main()
