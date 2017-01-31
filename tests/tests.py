"""
Integration tests for autopilotpattern/mysql. These tests are executed
inside a test-running container based on autopilotpattern/testing.
"""
from __future__ import print_function
import os
from os.path import expanduser
import random
import subprocess
import string
import sys
import time
import unittest
import uuid

from testcases import AutopilotPatternTest, WaitTimeoutError, dump_environment_to_file


class MySQLStackTest(AutopilotPatternTest):

    project_name = 'my'

    def setUp(self):
        """
        autopilotpattern/mysql setup.sh writes an _env file with a CNS
        entry and account info for Manta. If this has been mounted from
        the test environment, we'll use that, otherwise we have to
        generate it from the environment.
        """
        if not os.path.isfile('_env'):
            print('generating _env')
            os.environ['MYSQL_USER'] = self.user = 'mytestuser'
            os.environ['MYSQL_PASSWORD'] = self.passwd = gen_password()
            os.environ['MYSQL_DATABASE'] = self.db = 'mytestdb'
            os.environ['MYSQL_REPL_USER'] = self.repl_user = 'myrepluser'
            os.environ['MYSQL_REPL_PASSWORD'] = self.repl_passwd = gen_password()
            with open(os.environ['DOCKER_CERT_PATH'] + '/key.pem') as key_file:
                manta_key = '#'.join([line.strip() for line in key_file])
            os.environ['MANTA_PRIVATE_KEY'] = manta_key

            dump_environment_to_file('_env')

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
        # the primary; we need very long timeout b/c of provisioning
        self.settle('mysql-primary', 1, timeout=600)

        # scale up, make sure we have 2 working replica instances
        self.compose_scale('mysql', 3)
        self.settle('mysql', 2, timeout=600)

        # create a table
        create_table = 'CREATE TABLE tbl1 (field1 INT, field2 VARCHAR(36));'
        self.exec_query('mysql_1', create_table)

        # check replication is working by writing rows to the primary
        # and verifying they show up in the replicas

        insert_row = 'INSERT INTO tbl1 (field1, field2) VALUES ({}, "{}");'
        vals = [str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4())]

        self.exec_query('mysql_1', insert_row.format(1, vals[0]))
        self.exec_query('mysql_1', insert_row.format(1, vals[1]))
        self.assert_good_replication(vals[:2])

        # kill the primary, make sure we get a new primary
        self.docker_stop('mysql_1')
        self.settle('mysql-primary', 1, timeout=300)
        self.settle('mysql', 1)

        # check replication is still working
        primary = self.get_service_instances_from_consul('mysql-primary')[0]
        self.exec_query(primary, insert_row.format(1, vals[2]))
        self.assert_good_replication(vals)

    def settle(self, service, count, timeout=60):
        """
        Wait for the service to appear healthy and correctly in Consul
        """
        try:
            nodes = self.instrument(self.wait_for_service,
                                    service, count, timeout=timeout)
            if len(nodes) < count:
                raise WaitTimeoutError()
            self.instrument(self.assert_consul_correctness)
        except WaitTimeoutError:
            self.fail('Failed to scale {} to {} instances'
                      .format(service, count))

    def assert_consul_correctness(self):
        """ Verify that Consul addresses match container addresses """
        try:
            primary = self.get_primary_ip()
            replicas = self.get_replica_ips()
            expected = [str(ip) for ip in
                        self.get_service_ips('mysql', ignore_errors=True)[1]]
        except subprocess.CalledProcessError as ex:
            self.fail('subprocess.CalledProcessError: {}'.format(ex.output))
        expected.remove(primary)
        expected.sort()
        self.assertEqual(replicas, expected,
                         'Upstream blocks {} did not match actual IPs {}'
                         .format(replicas, expected))

    def assert_good_replication(self, expected_vals):
        """
        Checks each replica to make sure it has the recently written
        field2 values passed in as the `vals` param.
        """
        check_row = 'SELECT * FROM tbl1 WHERE `field1`=1;'

        def check_replica(replica):
            timeout = 15
            while timeout > 0:
                # we'll give the replica a couple chances to catch up
                results = self.exec_query(replica, check_row).splitlines()
                got_vals = []
                for line in results:
                    if line.startswith('field2:'):
                        got_vals.append(line.replace('field2: ', '', 1))
                    if not set(expected_vals) - set(got_vals):
                        return None # all values replicated

                # we're missing a value
                timeout -= 1
            return got_vals

        replicas = self.get_replica_containers()
        for replica in replicas:
            got_vals = check_replica(replica)
            if got_vals:
                self.fail('Replica {} is missing values {}; got {}'
                          .format(replica, expected_vals, got_vals))

    def get_primary_ip(self):
        """ Get the IP for the primary from Consul. """
        try:
            node = self.get_service_addresses_from_consul('mysql-primary')[0]
            return node
        except IndexError:
            self.fail('mysql-primary does not exist in Consul.')

    def get_replica_ips(self):
        """ Get the IPs for the replica(s) from Consul. """
        nodes = self.get_service_addresses_from_consul('mysql')
        nodes.sort()
        return nodes

    def get_primary_container(self):
        """ Get the container name for the primary from Consul """
        try:
            node = self.get_service_instances_from_consul('mysql-primary')[0]
            return node
        except IndexError:
            self.fail('mysql-primary does not exist in Consul.')

    def get_replica_containers(self):
        """ Get the container names for the replica(s) from Consul. """
        nodes = self.get_service_instances_from_consul('mysql')
        nodes.sort()
        return nodes

    def exec_query(self, container, query, user=None, passwd=None):
        """
        Runs SQL statement via docker exec. Normally this method would
        be subject to SQL injection but we control all inputs and we
        don't want to have to ship a mysql client in the test rig.
        """
        if not user:
            user = self.user
        if not passwd:
            passwd = self.passwd
        cmd = ['mysql', '-u', user,
               '-p{}'.format(passwd),
               '--vertical', # makes parsing easier
               '-e', query, self.db]
        try:
            out = self.docker_exec(container, cmd)
        except subprocess.CalledProcessError as ex:
            self.fail('subprocess.CalledProcessError in {} for command {}:\n{}'
                      .format(container, cmd, ex.output))
        return out


# ------------------------------------------------
# helper functions

def gen_password():
    """
    When we run the tests on Shippable the setup.sh fails silently
    and we end up with blank (unworkable) passwords. This appears
    to be specific to Shippable and not other Docker/Triton envs
    """
    return ''.join(random.choice(
        string.ascii_uppercase + string.digits) for _ in range(10))


if __name__ == "__main__":
    unittest.main(failfast=True)
