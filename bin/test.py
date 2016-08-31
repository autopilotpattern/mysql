import os
import socket
import unittest

import mock
import manage
from manage.containerpilot import ContainerPilot

manage.USE_STANDBY = False
HOST_NAME = socket.gethostname()
NAME = 'name'

class TestFuzzFailover(unittest.TestCase):

    def test_fuzz(self):
        pass

class TestContainerPilotConfig(unittest.TestCase):

    def setUp(self):
        self._old_consul = os.environ.get('CONSUL', '')
        self._old_consul_agent = os.environ.get('CONSUL_AGENT', '')
        os.environ['CONSUL'] = 'my.consul.example.com'

    def tearDown(self):
        os.environ['CONSUL'] = self._old_consul
        os.environ['CONSUL_AGENT'] = self._old_consul_agent

    def test_parse_with_consul_agent(self):
        os.environ['CONSUL_AGENT'] = '1'
        cp = ContainerPilot()
        cp.load(envs=os.environ)
        self.assertEqual(cp.config['consul'], 'localhost:8500')
        cmd = cp.config['coprocesses'][0]['command']
        host_cfg_idx = cmd.index('-retry-join') + 1
        self.assertEqual(cmd[host_cfg_idx], 'my.consul.example.com:8500')

    def test_parse_without_consul_agent(self):
        os.environ['CONSUL_AGENT'] = '0'
        cp = ContainerPilot()
        cp.load(envs=os.environ)
        self.assertEqual(cp.config['consul'], 'my.consul.example.com:8500')
        self.assertEqual(cp.config['coprocesses'], [])

        os.environ['CONSUL_AGENT'] = ''
        cp = ContainerPilot()
        cp.load(envs=os.environ)
        self.assertEqual(cp.config['consul'], 'my.consul.example.com:8500')
        self.assertEqual(cp.config['coprocesses'], [])


class TestAssertInitialization(unittest.TestCase):

    def setUp(self):
        self.patchers = [
            mock.patch('manage.run_as_primary', lambda node: None),
            mock.patch('manage.run_as_replica', lambda node: None),
            mock.patch('manage.get_primary_node', lambda: None),
            #mock.patch('manage.get_standby_node', lambda: None)
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        for f in ('/mysql-primary-init',
                  '/mysql-init', '/mysql-standby-init'):
            if os.path.exists(f):
                os.rmdir(f)

    @mock.patch('manage.get_primary_node', lambda: HOST_NAME)
    def test_lock_primary(self):
        with mock.patch('manage.run_as_primary') as runner:
            node = manage.Node()
            self.assertFalse(manage.assert_initialized_for_state(node))
            self.assertEqual(node.primary, HOST_NAME)

            node = manage.Node()
            self.assertTrue(manage.assert_initialized_for_state(node))
            runner.assert_called_once()

    # @mock.patch('manage.get_standby_node', lambda: HOST_NAME)
    # def test_lock_standby(self):
    #     with mock.patch('manage.run_as_standby') as runner:
    #         node = manage.Node()
    #         self.assertFalse(manage.assert_initialized_for_state(node))
    #         self.assertEqual(node.primary, None)
    #         self.assertEqual(node.standby, None)
    #         with mock.patch('manage.USE_STANDBY', True):
    #             node = manage.Node()
    #             self.assertFalse(manage.assert_initialized_for_state(node))
    #             self.assertEqual(node.primary, None)
    #             self.assertEqual(node.standby, HOST_NAME)

    #             node = manage.Node()
    #             self.assertTrue(manage.assert_initialized_for_state(node))
    #         runner.assert_called_once()

    @mock.patch('manage.get_primary_node', lambda: 'other')
    def test_lock_replica(self):
        with mock.patch('manage.run_as_replica') as runner:
            node = manage.Node()
            self.assertFalse(manage.assert_initialized_for_state(node))
            self.assertEqual(node.primary, 'other')

            node = manage.Node()
            self.assertTrue(manage.assert_initialized_for_state(node))
            runner.assert_called_once()




if __name__ == '__main__':
    unittest.main()
