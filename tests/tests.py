from __future__ import print_function
import os
import re
import subprocess
import time
import unittest
from testcases import AutopilotPatternTest, WaitTimeoutError, \
    dump_environment_to_file

class MySQLStackTest(AutopilotPatternTest):

    project_name = 'my'
    compose_file = 'local-compose.yml'

    def test_replication_and_failover(self):
        """
        Given the MySQL stack, when we scale up MySQL instances they should:
        - become a new replica
        - with working replication
        Given when we stop the MySQL primary:
        - one of the replicas should become the new primary
        - the other replica should replicate from it
        """
        # wait until the first instance has configured itself as the
        # the primary
        self.instrument(self.settle, 'mysql-primary', 1)

        # scale up, make sure we have 2 working replica instances
        self.compose_scale('mysql', 3)
        self.instrument(self.settle, 'mysql', 2, timeout=90)
        self.instrument(self.check)

        # check replication is working
        user = os.environ.get('MYSQL_USER')
        passwd = os.environ.get('MYSQL_PASSWORD')
        db = os.environ.get('MYSQL_DATABASE')
        out = self.docker_exec(
            'mysql_1',
            ['mysql', '-u', user, '-p{}'.format(passwd), '-e', 'SELECT 1', db]
        )

        # kill the primary, make sure we get a new primary
        self.docker_stop('mysql_1')
        self.instrument(self.settle, 'mysql-primary', 1, timeout=150)
        self.instrument(self.settle, 'mysql', 1)

        # check replication is working
        # TODO


    def settle(self, service, count, timeout=60):
        """ Wait for the service to appear as healthy in Consul """
        try:
            nodes = self.wait_for_service(service, count, timeout=timeout)
            if len(nodes) < count:
                raise WaitTimeoutError()
        except WaitTimeoutError:
            self.fail('Failed to scale {} to {} instances'
                      .format(service, count))

    def check(self):
        """ Verify that Consul addresses match container addresses """
        replicas = self.get_replicas()
        primary = self.get_primary()

        expected = [str(ip) for ip in self.get_service_ips('mysql')[1]]
        expected.remove(primary)
        expected.sort()

        self.assertEqual(replicas, expected,
                         'Upstream blocks {} did not match actual IPs {}'
                         .format(replicas, expected))

    def get_primary(self):
        """ Get the IP for the primary from Consul. """
        nodes = self.get_service_addresses_from_consul('mysql-primary')
        if len(nodes) != 1:
            self.fail()
        return nodes[0]

    def get_replicas(self):
        """ Get the IPs for the replica(s) from Consul. """
        nodes = self.get_service_addresses_from_consul('mysql')
        nodes.sort()
        return nodes


if __name__ == "__main__":
    dump_environment_to_file('/src/_env')
    unittest.main()
