from __future__ import print_function
import re
import time
import unittest
from testcases import AutopilotPatternTest, WaitTimeoutError, debug

class MySQLStackTest(AutopilotPatternTest):

    project_name = 'my'

    @debug
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
        self.settle('mysql-primary', 1)

        # scale up, make sure we have 2 working replica instances
        self.docker_compose_scale('mysql', 3)
        self.settle('mysql', 2)
        self.check()

        # check replication is working
        # TODO

        # kill the primary, make sure we get a new primary
        # TODO

        # check replication is working
        # TODO


    def settle(self, service, count):
        """ Wait for the service to appear as healthy in Consul """
        nodes = self.wait_for_service(service, count, timeout=60)
        if len(nodes) < count:
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
    unittest.main()
