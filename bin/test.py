import os
import unittest

import mock
import manage

manage.USE_STANDBY = False
NAME = manage.get_name()

class TestAssertInitialization(unittest.TestCase):

    def setUp(self):
        self.patchers = [
            mock.patch('manage.run_as_primary', lambda node: None),
            mock.patch('manage.run_as_standby', lambda node: None),
            mock.patch('manage.run_as_replica', lambda node: None),
            mock.patch('manage.get_primary_node', lambda: None),
            mock.patch('manage.get_standby_node', lambda: None)
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

    @mock.patch('manage.get_primary_node', lambda: NAME)
    def test_lock_primary(self):
        with mock.patch('manage.run_as_primary') as runner:
            node = manage.MySQLNode()
            self.assertFalse(manage.assert_initialized_for_state(node))
            self.assertEqual(node.primary, NAME)
            self.assertTrue(manage.assert_initialized_for_state(node))
            runner.assert_called_once()

    @mock.patch('manage.get_standby_node', lambda: NAME)
    def test_lock_standby(self):
        with mock.patch('manage.run_as_standby') as runner:
            node = manage.MySQLNode()
            self.assertFalse(manage.assert_initialized_for_state(node))
            self.assertEqual(node.primary, None)
            self.assertEqual(node.standby, None)
            with mock.patch('manage.USE_STANDBY', True):
                self.assertFalse(manage.assert_initialized_for_state(node))
                self.assertEqual(node.primary, None)
                self.assertEqual(node.standby, NAME)
                self.assertTrue(manage.assert_initialized_for_state(node))
            runner.assert_called_once()

    @mock.patch('manage.get_primary_node', lambda: 'other')
    def test_lock_replica(self):
        with mock.patch('manage.run_as_replica') as runner:
            node = manage.MySQLNode()
            self.assertFalse(manage.assert_initialized_for_state(node))
            self.assertEqual(node.primary, 'other')
            self.assertTrue(manage.assert_initialized_for_state(node))
            runner.assert_called_once()




if __name__ == '__main__':
    unittest.main()
