import json
from Queue import Queue as Queue
from Queue import Empty, Full
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

# pylint: disable=import-error
import consul as pyconsul
import mock

import manage
from manage.containerpilot import ContainerPilot
from manage.libconsul import Consul
from manage.libmanta import Manta
from manage.libmysql import MySQL
from manage.utils import *


thread_data = threading.local()

def trace_wait(frame, event, arg):
    if event != 'call':
        return
    func_name = frame.f_code.co_name
    if func_name in thread_data.trace_into or func_name == '_mock_call':
        node_name = threading.current_thread().name
        timeout = thread_data.timeout
        try:
            # block until new work is available but no more than 5
            # seconds before killing the thread
            val = thread_data.queue.get(True, timeout)
            if val == 'quit':
                raise Empty
            thread_data.queue.task_done()
        except Empty:
            sys.exit(0)


class TestNode(object):
    """
    TestNode is a context containing a manage.Node object and which runs
    the function under test in its own thread. The function under test
    is stepped thru with each `tick()` call on this object.
    """
    def __init__(self, node=None, fn=None, trace_into=[]):
        self.node = node
        self._nodeq = Queue(1)
        self._thread = threading.Thread(
            target=self.step_thru,
            name=node.name,
            args=(fn, node),
            kwargs=dict(queue=self._nodeq, trace_into=trace_into))
        self._thread.start()

    def step_thru(self, fn, *args, **kwargs):
        """
        Step thru the function `fn`. The function will block every time it
        calls a function w/ a name listed in `trace_into` until a message
        is pushed onto the `queue` or a number of seconds equal to
        `test_timeout` passes. Exceptions are allowed to bubble up and
        crash the thread.
        """
        thread_data.queue = kwargs.pop('queue')
        thread_data.trace_into = kwargs.pop('trace_into', None)
        thread_data.timeout = kwargs.pop('test_timeout', 5)
        sys.settrace(trace_wait)
        fn(*args, **kwargs)

    def tick(self, val=True):
        print('tick!: {}'.format(val))
        try:
            self._nodeq.put(val, True, 1)
        except Full:
            raise Full('{} raised Full at {}'.format(self.node.name, val))
        time.sleep(0.01) # ensure we yield the main thread

    def end(self):
        try:
            self._nodeq.put('quit', True, 1)
        except Full:
            pass
        finally:
            self._thread.join()


class TestPreStart(unittest.TestCase):

    def setUp(self):
        logging.getLogger('manage').setLevel(logging.WARN)
        consul = mock.MagicMock()
        manta = mock.MagicMock()
        my = mock.MagicMock()
        my.datadir = tempfile.mkdtemp()
        _node1 = manage.Node(consul=consul, manta=manta, mysql=my)
        trace_into=['render', 'has_snapshot', 'get_backup',
                    'restore_from_snapshot', 'initialize_db', 'get']
        self.node_1 = TestNode(node=_node1, fn=manage.pre_start,
                               trace_into=trace_into)

    def tearDown(self):
        logging.getLogger('manage').setLevel(logging.DEBUG)
        self.node_1.end()

    def test_pre_start_first_node(self):
        """
        The first node will not attempt to download a snapshot from Manta.
        """
        self.node_1.node.consul.has_snapshot.return_value = False
        manage.pre_start(self.node_1.node)
        self.node_1.node.consul.has_snapshot.assert_called_once()
        self.node_1.node.mysql.initialize_db.assert_called_once()
        self.assertFalse(self.node_1.node.manta.get_backup.called)
        self.assertFalse(self.node_1.node.mysql.restore_from_snapshot.called)

    def test_pre_start_snapshot_complete(self):
        """
        Given a successful snapshot by the first node, a new node will
        download the snapshot from Manta
        """
        self.node_1.node.consul.has_snapshot.return_value = True
        manage.pre_start(self.node_1.node)
        self.node_1.node.consul.has_snapshot.assert_called_once()
        self.node_1.node.manta.get_backup.assert_called_once()
        self.node_1.node.mysql.restore_from_snapshot.assert_called_once()
        self.assertFalse(self.node_1.node.mysql.initialize_db.called)

    def test_pre_start_no_reinitialization(self):
        """
        Given a node that's restarted, pre_start should not try
        to re-initialize the node.
        """
        os.mkdir(os.path.join(self.node_1.node.mysql.datadir, 'mysql'))
        self.node_1.node.consul.has_snapshot.return_value = True
        manage.pre_start(self.node_1.node)
        self.assertFalse(self.node_1.node.consul.has_snapshot.called)

    def test_pre_start_snapshot_incomplete(self):
        """
        Given a snapshot that has been marked successful but not
        completed, a new node will wait and not crash.
        """
        self.node_1.node.consul = Consul(TEST_ENVIRON)
        self.node_1.node.consul.client = mock.MagicMock()
        self.node_1.node.consul.client.kv.get.side_effect = pyconsul.ConsulException('')

        self.node_1.tick('pre_start')
        self.node_1.tick('render')
        self.node_1.tick('has_snapshot')
        self.node_1.tick('get')
        self.node_1.tick('get')
        self.node_1.node.consul.client.kv.get.side_effect = None
        self.node_1.node.consul.client.kv.get.return_value = [0,{'Value': 'ok'}]
        self.node_1.tick('get_backup')
        self.node_1.tick('restore_from_snapshot')
        self.node_1.end()

        self.node_1.node.manta.get_backup.assert_called_once()
        self.assertEqual(self.node_1.node.consul.client.kv.get.call_count, 2)
        self.node_1.node.mysql.restore_from_snapshot.assert_called_once()
        self.assertFalse(self.node_1.node.mysql.initialize_db.called)


class TestHealth(unittest.TestCase):
    pass

class TestOnChange(unittest.TestCase):
    pass

class TestSnapshotTask(unittest.TestCase):

    def setUp(self):
        logging.getLogger('manage').setLevel(logging.WARN)
        consul = mock.MagicMock()
        manta = mock.MagicMock()
        my = mock.MagicMock()
        cp = ContainerPilot()
        cp.state = PRIMARY
        my.datadir = tempfile.mkdtemp()
        self.node = manage.Node(consul=consul, cp=cp, manta=manta, mysql=my)

    def tearDown(self):
        logging.getLogger('manage').setLevel(logging.DEBUG)
        try:
            os.remove('/tmp/mysql-backup-run')
        except OSError as ex:
            pass

    def test_not_snapshot_node(self):
        """ Don't snapshot if this isn't the snapshot node """
        # TODO update when this logic changes
        self.node.cp.state = REPLICA
        manage.snapshot_task(self.node)
        self.assertFalse(self.node.mysql.query.called)

    def test_binlog_stale(self):
        """ Snapshot if the binlog is stale even if its not time to do so """
        self.node.consul.is_check_healthy.return_value = True
        self.node.consul.get.return_value = 'mybackup1'
        self.node.mysql.query.return_value = [['mybackup2']]

        with mock.patch('manage.write_snapshot') as ws:
            manage.snapshot_task(self.node)
            self.node.mysql.query.assert_called_once()
            self.node.consul.get.assert_called_once()
            self.assertTrue(ws.called)

    def test_backup_already_running(self):
        """ Don't snapshot if there's already a snapshot running """
        self.node.consul.is_check_healthy.return_value = False
        self.node.consul.get.return_value = 'mybackup1'
        self.node.mysql.query.return_value = [['mybackup2']]

        with mock.patch('manage.write_snapshot') as ws:
            lockfile_name = '/tmp/mysql-backup-run'
            try:
                backup_lock = open(lockfile_name, 'w')
                fcntl.flock(backup_lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
                manage.snapshot_task(self.node)
            finally:
                fcntl.flock(backup_lock, fcntl.LOCK_UN)
                backup_lock.close()
            self.assertFalse(ws.called)

    def test_time_to_snapshot(self):
        """ Snapshot if the timer has elapsed even if the binlog isn't stale"""
        self.node.consul.is_check_healthy.return_value = False
        self.node.consul.get.return_value = 'mybackup1'
        self.node.mysql.query.return_value = [['mybackup1']]
        with mock.patch('manage.write_snapshot') as ws:
            manage.snapshot_task(self.node)
            self.assertTrue(ws.called)


class TestMySQL(unittest.TestCase):

    def setUp(self):
        logging.getLogger('manage').setLevel(logging.WARN)
        self.environ = TEST_ENVIRON.copy()
        self.my = MySQL(self.environ)
        self.my._conn = mock.MagicMock()

    def tearDown(self):
        logging.getLogger('manage').setLevel(logging.DEBUG)

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
            mock.call.cursor().execute('query 2', params=()),
            mock.call.commit(),
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
            mock.call.cursor().execute('query 4', params=()),
            mock.call.cursor().execute('query 5', params=()),
            mock.call.commit(),
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
        self.environ = TEST_ENVIRON.copy()

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
        logging.getLogger('manage').setLevel(logging.WARN)
        self.environ = TEST_ENVIRON.copy()

    def tearDown(self):
        logging.getLogger('manage').setLevel(logging.DEBUG)

    def test_parse_with_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '1'
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        self.assertEqual(cp.config['consul'], 'localhost:8500')
        cmd = cp.config['coprocesses'][0]['command']
        host_cfg_idx = cmd.index('-retry-join') + 1
        self.assertEqual(cmd[host_cfg_idx], 'my.consul.example.com:8500')
        self.assertEqual(cp.state, REPLICA)

    def test_parse_without_consul_agent(self):
        self.environ['CONSUL_AGENT'] = '0'
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        self.assertEqual(cp.config['consul'], 'my.consul.example.com:8500')
        self.assertEqual(cp.config['coprocesses'], [])
        self.assertEqual(cp.state, REPLICA)

        self.environ['CONSUL_AGENT'] = ''
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        self.assertEqual(cp.config['consul'], 'my.consul.example.com:8500')
        self.assertEqual(cp.config['coprocesses'], [])
        self.assertEqual(cp.state, REPLICA)

    def test_update(self):
        self.environ['CONSUL_AGENT'] = '1'
        cp = ContainerPilot()
        cp.load(envs=self.environ)
        temp_file = tempfile.NamedTemporaryFile()
        cp.path = temp_file.name

        # no update expected
        cp.update()
        with open(temp_file.name, 'r') as updated:
            self.assertEqual(updated.read(), '')

        # force an update
        cp.state = UNASSIGNED
        cp.update()
        with open(temp_file.name, 'r') as updated:
            config = json.loads(updated.read())
            self.assertEqual(config['consul'], 'localhost:8500')
            cmd = config['coprocesses'][0]['command']
            host_cfg_idx = cmd.index('-retry-join') + 1
            self.assertEqual(cmd[host_cfg_idx], 'my.consul.example.com:8500')


class TestMantaConfig(unittest.TestCase):

    def setUp(self):
        self.environ = TEST_ENVIRON.copy()

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

    'CONTAINERPILOT': 'file:///etc/containerpilot.json',

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



if __name__ == '__main__':
    unittest.main()
