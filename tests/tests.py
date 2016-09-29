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

from testcases import AutopilotPatternTest, WaitTimeoutError

UNIT_ONLY=False

@unittest.skipIf(UNIT_ONLY, "running only unit tests")
class MySQLStackTest(AutopilotPatternTest):

    project_name = 'my'

    def setUp(self):
        """
        autopilotpattern/mysql setup.sh writes an _env file with a CNS
        entry and account info for Manta. In local-only testing this fails
        so we need to set up a temporary remote Docker env
        """
        self.set_remote_docker_env()

        try:
            home = expanduser("~")
            key = '{}/.ssh/id_rsa'.format(home)
            self.run_script('ln', '-s', '/tmp/ssh/TritonTestingKey', key)
            pub = self.run_script('/usr/bin/ssh-keygen','-y','-f', key)
            with open('{}.pub'.format(key), 'w') as f:
                f.write(pub)
            self.run_script('/src/setup.sh', '{}/.ssh/id_rsa'.format(home))
        except subprocess.CalledProcessError as ex:
            self.fail(ex.output)

        _manta_url =os.environ.get('MANTA_URL', 'https://us-east.manta.joyent.com')
        _manta_user = os.environ.get('MANTA_USER', 'triton_mysql')
        _manta_subuser = os.environ.get('MANTA_SUBUSER', 'triton_mysql')
        _manta_role = os.environ.get('MANTA_ROLE', 'triton_mysql')
        _manta_bucket = os.environ.get('MANTA_BUCKET',
                                       '/{}/stor/{}'.format(_manta_user, _manta_subuser))

        self.update_env_file(
            '_env', (
                ('MYSQL_PASSWORD', gen_password()),
                ('MYSQL_REPL_PASSWORD', gen_password()),
                ('MANTA_URL', _manta_url),
                ('MANTA_USER', _manta_user),
                ('MANTA_SUBUSER', _manta_subuser),
                ('MANTA_ROLE', _manta_role),
                ('MANTA_BUCKET', _manta_bucket),
            )
        )

        self.restore_local_docker_env()

        # read-in the env file so we can use it in tests
        env = self.read_env_file('_env')
        self.user = env.get('MYSQL_USER')
        self.passwd = env.get('MYSQL_PASSWORD')
        self.db = env.get('MYSQL_DATABASE')
        self.repl_user = env.get('MYSQL_REPL_USER')
        self.repl_passwd = env.get('MYSQL_REPL_PASSWORD')

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
        self.settle('mysql-primary', 1, timeout=120)

        # scale up, make sure we have 2 working replica instances
        self.compose_scale('mysql', 3)
        self.settle('mysql', 2, timeout=180)

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
        self.settle('mysql-primary', 1, timeout=60)
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
                        got_vals.append(line.lstrip('field2: '))
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

    if len(sys.argv) > 1:
        if sys.argv[1] == "unit":
            UNIT_ONLY=True
    unittest.main(failfast=True)
