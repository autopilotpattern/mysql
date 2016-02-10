"""
Test client for triton-mysql.py
"""
from __future__ import print_function
import logging
import os
import sys
import time

import pymysql
import consul as pyconsul

# stream the logs to stdout so that we can pick them up via Docker logs
logging.basicConfig(format='%(asctime)s %(levelname)s %(name)s %(message)s',
                    stream=sys.stdout,
                    level=logging.getLevelName(
                        os.environ.get('LOG_LEVEL', 'DEBUG')))
logging.getLogger('requests').setLevel(logging.WARN)
log = logging.getLogger('triton-mysql')

consul = pyconsul.Consul(host=os.environ.get('TRITON_MYSQL_CONSUL', 'consul'))
config = None

# ---------------------------------------------------------

class MySQLConfig(object):
    """
    MySQLConfig is where we store and access environment variables and render
    the mysqld configuration file based on those values.
    """

    def __init__(self):
        self.mysql_db = os.environ.get('MYSQL_DATABASE', None)
        self.mysql_user = os.environ.get('MYSQL_USER', None)
        self.mysql_password = os.environ.get('MYSQL_PASSWORD', None)
        self.mysql_root_password = os.environ.get('MYSQL_ROOT_PASSWORD', '')
        self.repl_user = os.environ.get('MYSQL_REPL_USER', None)
        self.repl_password = os.environ.get('MYSQL_REPL_PASSWORD', None)

# ---------------------------------------------------------

def main():

    primary_host = get_primary()
    primary_ctx = dict(ip=primary_host,
                       user=config.repl_user,
                       password=config.repl_password,
                       database=config.mysql_db)
    primary = wait_for_connection(**primary_ctx)

    replica_hosts = get_replicas()
    replicas = []
    for ip in replica_hosts:
        ctx = dict(ip=primary_host,
                   user=config.repl_user,
                   password=config.repl_password,
                   database=config.mysql_db)
        replicas.append(wait_for_connection(**ctx))







def get_primary():
    """ Get the IP for the primary from Consul. """
    nodes = consul.health.service('mysql-primary', passing=True)[1]
    ips = [service['Service']['Address'] for service in nodes]
    assert len(ips) == 1
    return ips[0]

def get_replicas():
    """ Get the IPs for the replica(s) from Consul. """
    nodes = consul.health.service('mysql', passing=True)[1]
    ips = [service['Service']['Address'] for service in nodes]
    assert len(ips) != 0
    return ips

def wait_for_connection(ip, user='root', password=None, database=None, timeout=30):
    while timeout > 0:
        try:
            return pymysql.connect(host=ip,
                                   user=user,
                                   password=password,
                                   database=database,
                                   charset='utf8',
                                   connect_timeout=timeout)
        except pymysql.err.OperationalError:
            timeout = timeout - 1
            if timeout == 0:
                raise
            time.sleep(1)

# ---------------------------------------------------------
# utility functions

def mysql_exec(conn, sql, params):
    try:
        with conn.cursor() as cursor:
            log.debug(sql)
            log.debug(params)
            cursor.execute(sql, params)
            conn.commit()
    except Exception:
        raise # re-raise so that we exit

def mysql_query(conn, sql, params):
    try:
        with conn.cursor() as cursor:
            log.debug(sql)
            log.debug(params)
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Exception:
        raise # re-raise so that we exit

# ---------------------------------------------------------

if __name__ == '__main__':

    config = MySQLConfig()

    if len(sys.argv) > 1:
        try:
            locals()[sys.argv[1]]()
        except KeyError:
            log.error('Invalid command %s', sys.argv[1])
            sys.exit(1)
    else:
        main()
