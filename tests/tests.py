from __future__ import print_function
import os
import re
import subprocess
import time
import unittest
import uuid

from testcases import AutopilotPatternTest, WaitTimeoutError, \
    dump_environment_to_file

class MySQLStackTest(AutopilotPatternTest):

    project_name = 'my'

    def setUp(self):
        self.user = os.environ.get('MYSQL_USER')
        self.passwd = os.environ.get('MYSQL_PASSWORD')
        self.db = os.environ.get('MYSQL_DATABASE')
        self.repl_user = os.environ.get('MYSQL_REPL_USER')
        self.repl_passwd = os.environ.get('MYSQL_REPL_PASSWORD')

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
        self.compose_scale('mysql', 3)
        self.settle('mysql', 2, timeout=90)

        # create a table
        create_table = 'CREATE TABLE tbl1 (field1 INT, field2 VARCHAR(36));'
        self.exec_query('mysql_1', create_table)

        # check replication is working by writing rows to the primary
        # and verifying they show up in the replicas

        insert_row = "INSERT INTO tbl1 VALUES ({},'{}');"
        vals = [str(uuid.uuid4()),
                str(uuid.uuid4()),
                str(uuid.uuid4())]

        self.exec_query('mysql_1', insert_row.format(1,vals[0]))
        self.exec_query('mysql_1', insert_row.format(1,vals[1]))
        self.assert_good_replication(vals[:2])

        # kill the primary, make sure we get a new primary
        self.docker_stop('mysql_1')
        self.settle('mysql-primary', 1, timeout=150)
        self.settle('mysql', 1)

        # check replication is still working
        primary = self.get_service_instances_from_consul('mysql-primary')[0]
        self.exec_query(primary, insert_row.format(1,vals[2]))
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

    def assert_good_replication(self, vals):
        """
        Checks each replica to make sure it has the recently written
        field2 values passed in as the `vals` param.
        """
        check_row = 'SELECT * FROM tbl1 WHERE field1 = {};'
        check_replica = 'SHOW SLAVE STATUS\G;'
        replicas = self.get_replica_containers()
        for replica in replicas:
            timeout = 10
            while timeout > 0:
                # make sure the replica has had a chance to catch up
                out = self.exec_query(replica, check_replica,
                                      user=self.repl_user,
                                      passwd=self.repl_passwd)
                if 'Waiting for master to send event' in out:
                    break
                time.sleep(1)
                timeout -= 1
            else:
                self.fail("Timed out waiting for replication to catch up")

            out = self.exec_query(replica, check_row.format(1))
            query_result = self.parse_query(out)
            try:
                query_vals = [row['field2'] for row in query_result]
                self.assertEqual(len(query_vals), len(vals))
                for val in vals:
                    self.assertIn(val, query_vals)
            except (KeyError, IndexError):
                self.fail('Missing expected results:\n{}'.format(query_result))

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
               '-e', query, self.db]
        try:
            out = self.docker_exec(container, cmd)
        except subprocess.CalledProcessError as ex:
            self.fail('subprocess.CalledProcessError in {} for command {}:\n{}'
                      .format(container, cmd, ex.output))
        return out

    def parse_query(self, result):
        """ parse the query result """
        ignore_warn = ('Warning: Using a password on the command line '+
                       'interface can be insecure.\n')
        result = result.lstrip(ignore_warn)

        res = result.splitlines()
        fields = [r.strip() for r in res[0].split('\t')]
        rows = res[1:]
        if not rows:
            raise Exception('No results found.')
        parsed = []
        for row in rows:
            cols = [col.strip() for col in row.split('\t')]
            row_dict = {}
            for i, field in enumerate(fields):
                row_dict[field] = cols[i]
            parsed.append(row_dict)
        return parsed


if __name__ == "__main__":
    dump_environment_to_file('/src/_env')
    unittest.main()
