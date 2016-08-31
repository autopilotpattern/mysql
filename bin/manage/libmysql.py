""" autopilotpattern/mysql MySQL module """
from collections import OrderedDict
import os
import re
import pwd
import socket
import subprocess
import string
import time
from manage.libconsul import get_primary_host
from manage.utils import debug, env, log, get_ip, to_flag, \
    WaitTimeoutError

# pylint: disable=import-error,no-self-use,invalid-name,dangerous-default-value
import mysql.connector as mysqlconn
from mysql.connector import Error as MySQLError

class MySQL(object):
    """
    MySQL represents the connection to and configuration of the MySQL
    process and its clients.
    """
    def __init__(self, envs=os.environ):
        self.mysql_db = env('MYSQL_DATABASE', None, envs)
        self.mysql_user = env('MYSQL_USER', None, envs)
        self.mysql_password = env('MYSQL_PASSWORD', None, envs)
        self.mysql_root_password = env('MYSQL_ROOT_PASSWORD', '', envs)
        self.mysql_random_root_password = env('MYSQL_RANDOM_ROOT_PASSWORD',
                                              False, envs, to_flag)
        self.mysql_onetime_password = env('MYSQL_ONETIME_PASSWORD',
                                          False, envs, to_flag)
        self.repl_user = env('MYSQL_REPL_USER', None, envs)
        self.repl_password = env('MYSQL_REPL_PASSWORD', None, envs)
        self.datadir = env('MYSQL_DATADIR', '/var/lib/mysql', envs)
        self.pool_size = env('INNODB_BUFFER_POOL_SIZE', 0, envs, fn=int)

        # state
        self.ip = get_ip()
        self._conn = None
        self._query_buffer = OrderedDict()

    def render(self, src='/etc/my.cnf.tmpl', dest='/etc/my.cnf'):
        """
        Writes-out config files, even if we've previously initialized the DB,
        so that we can account for changed hostnames, resized containers, etc.
        """
        pool_size = self._get_innodb_buffer_pool_size()
        with open(src, 'r') as f:
            template = string.Template(f.read())
            rendered = template.substitute(buffer=pool_size,
                                           server_id=self.server_id,
                                           hostname=self.ip)
        with open(dest, 'w') as f:
            f.write(rendered)

    @property
    def server_id(self):
        """ replace server-id with ID derived from hostname """
        _hostname = socket.gethostname()
        return int(str(_hostname)[:4], 16)

    def _get_innodb_buffer_pool_size(self):
        """
        replace innodb_buffer_pool_size value from environment
        or use a sensible default (70% of available physical memory)
        """
        if not self.pool_size:
            with open('/proc/meminfo', 'r') as memInfoFile:
                memInfo = memInfoFile.read()
                base = re.search(r'^MemTotal: *(\d+)', memInfo).group(1)
                self.pool_size = int((int(base) / 1024) * 0.7)
        return self.pool_size

    @property
    def conn(self):
        """
        Convenience method for setting up a cached connection
        with the replication manager user.
        """
        if self._conn:
            return self._conn
        ctx = dict(user=self.repl_user,
                   password=self.repl_password,
                   timeout=25) # derived from ContainerPilot config ttl
        self._conn = self.wait_for_connection(**ctx)
        return self._conn

    @debug(name='mysql.wait_for_connection')
    def wait_for_connection(self, user='root', password=None, database=None,
                            timeout=30):
        """
        Polls mysqld socket until we get a connection or the timeout
        expires (raise WaitTimeoutError). Defaults to root empty/password.
        """
        while timeout > 0:
            try:
                sock = '/var/run/mysqld/mysqld.sock'
                return mysqlconn.connect(unix_socket=sock,
                                         user=user,
                                         password=password,
                                         database=database,
                                         charset='utf8',
                                         connection_timeout=timeout)
            except MySQLError as ex:
                timeout = timeout - 1
                if timeout == 0:
                    raise WaitTimeoutError(ex)
                time.sleep(1)

    def add(self, stmt, params):
        """ Adds a new SQL statement to an internal query buffer """
        self._query_buffer[stmt] = params

    @debug(name='mysql.execute')
    def execute(self, sql, params=(), conn=None):
        """ Execute and commit a SQL statement with parameters """
        self.add(sql, params)
        self._execute(conn, results=False, commit=True)

    @debug(name='mysql.execute_many')
    def execute_many(self, conn=None):
        """
        Execute and commit all previously `add`ed statements
        in the query buffer
        """
        self._execute(conn, results=False, commit=True)

    @debug(name='mysql.query')
    def query(self, sql, params=(), conn=None):
        """ Execute a SQL query with params and return results. """
        self.add(sql, params)
        return self._execute(conn=conn, results=True, commit=False)

    def _execute(self, conn=None, results=False, commit=True):
        """
        Execute and commit all composed statements and flushes the buffer
        """
        try:
            if not conn:
                conn = self.conn
            cur = conn.cursor()
            for stmt, params in self._query_buffer.items():
                log.debug(stmt)
                log.debug(params)
                cur.execute(stmt, params=params)
            if commit:
                conn.commit()
            if results:
                return cur.fetchall()
        except MySQLError:
            raise # this is an unrecoverable situation
        finally:
            self._query_buffer.clear()
            cur.close()

    @debug(name='mysql.initialize_db', log_output=True)
    def initialize_db(self):
        """
        post-installation run to set up data directories
        and install mysql.user tables
        """
        self.make_datadir()
        log.info('Initializing database...')
        try:
            subprocess.check_call(['/usr/bin/mysql_install_db',
                                   '--user=mysql',
                                   '--datadir={}'.format(self.datadir)])
            log.info('Database initialized.')
            return True
        except subprocess.CalledProcessError:
            log.warn('Database was previously initialized.')
            return False

    def make_datadir(self):
        """ Create the data dir if it doesn't already exist"""
        try:
            os.mkdir(self.datadir, 0770)
            self.take_ownership()
        except OSError:
            pass

    def take_ownership(self, owner='mysql'):
        """
        Set ownership of all directories and files under config.datadir
        to `owner`'s UID and GID. Defaults to setting ownership for
        mysql user.
        """
        directory = self.datadir
        user = pwd.getpwnam(owner)
        os.chown(directory, user.pw_uid, user.pw_gid)
        for root, dirs, files in os.walk(self.datadir):
            for di in dirs:
                os.chown(os.path.join(root, di), user.pw_uid, user.pw_gid)
            for fi in files:
                os.chown(os.path.join(root, fi), user.pw_uid, user.pw_gid)

    def setup_root_user(self, conn):
        """
        Create the root user and optionally give it a random root password
        """
        if self.mysql_random_root_password:
            # we could use --random-passwords in our call to `mysql_install_db`
            # instead here but we want to have the root password available
            # until we're done with this setup.
            chars = string.ascii_letters + string.digits + '!@#$%&^*()'
            passwd = ''.join([chars[int(os.urandom(1).encode('hex'), 16) % len(chars)]
                              for _ in range(20)])
            self.mysql_root_password = passwd
            log.info('Generated root password: %s', self.mysql_root_password)

        self.add('SET @@SESSION.SQL_LOG_BIN=0;', ())
        self.add('DELETE FROM `mysql`.`user` where user != \'mysql.sys\';', ())
        self.add('CREATE USER `root`@`%` IDENTIFIED BY %s ;',
                 (self.mysql_root_password,))
        self.add('GRANT ALL ON *.* TO `root`@`%` WITH GRANT OPTION ;', ())
        self.add('DROP DATABASE IF EXISTS test ;', ())
        self.add('FLUSH PRIVILEGES ;', ())
        self.execute_many(conn=conn)

    def expire_root_password(self, conn):
        """ optionally expire the root password """
        if self.mysql_onetime_password:
            self.execute('ALTER USER `root`@`%` PASSWORD EXPIRE', conn=conn)

    def create_db(self, conn):
        """ this optional schema will be used by the application """
        if not self.mysql_db:
            log.warn('No default database configured.')
            return
        sql = 'CREATE DATABASE IF NOT EXISTS `{}`;'.format(self.mysql_db)
        self.execute(sql, conn=conn)

    def create_default_user(self, conn):
        """ this optional user will be used by the application """
        if not self.mysql_user or not self.mysql_password:
            log.warn('No default user/password configured.')
            return

        # there's some kind of annoying encoding bug in the lib here
        # so we have to format the string rather than passing it as
        # a param. totally safe, I bet.
        self.add('CREATE USER `{}`@`%` IDENTIFIED BY %s;'
                 .format(self.mysql_user), (self.mysql_password,))
        if self.mysql_db:
            self.add('GRANT ALL ON `{}`.* TO `{}`@`%`;'
                     .format(self.mysql_db, self.mysql_user), ())
        self.add('FLUSH PRIVILEGES;', ())
        self.execute_many(conn=conn)

    def create_repl_user(self, conn):
        """ this user will be used for both replication and backups """
        if not self.repl_user or not self.repl_password:
            log.error('No replication user/password configured.')
            return

        self.add('CREATE USER `{}`@`%` IDENTIFIED BY %s; '
                 .format(self.repl_user), (self.repl_password,))
        self.add('GRANT SUPER, SELECT, INSERT, REPLICATION SLAVE, RELOAD'
                 ', LOCK TABLES, GRANT OPTION, REPLICATION CLIENT'
                 ', RELOAD, DROP, CREATE '
                 'ON *.* TO `{}`@`%`; '
                 .format(self.repl_user), ())
        self.add('FLUSH PRIVILEGES;', ())
        self.execute_many(conn=conn)

    def set_timezone_info(self):
        """
        Write TZ data to mysqld by piping mysql_tzinfo_to_sql to the mysql
        client. This is kinda gross but piping it avoids having to parse the
        output for a bulk insert with the Connector/MySQL client.
        """
        try:
            subprocess.check_output(
                '/usr/bin/mysql_tzinfo_to_sql /usr/share/zoneinfo | '
                '/usr/bin/mysql -uroot --protocol=socket '
                '--socket=/var/run/mysqld/mysql.sock')
        except (subprocess.CalledProcessError, OSError):
            log.error('mysql_tzinfo_to_sql returned error.')

    def restore_from_snapshot(self, filename):
        """
        Use innobackupex to restore from a snapshot.
        """
        self.make_datadir()
        infile = '/tmp/backup/{}'.format(filename)
        subprocess.check_call(['tar', '-xif', infile, '-C', '/tmp/backup'])
        subprocess.check_call(['/usr/bin/innobackupex',
                               '--force-non-empty-directories',
                               '--copy-back',
                               '/tmp/backup'])
        self.take_ownership()



@debug
def set_primary_for_replica(node):
    """
    Set up GTID-based replication to the primary; once this is set the
    replica will automatically try to catch up with the primary's last
    transactions.
    """
    primary = get_primary_host(node.consul)
    node.mysql.add('CHANGE MASTER TO '
                   'MASTER_HOST           = %s, '
                   'MASTER_USER           = %s, '
                   'MASTER_PASSWORD       = %s, '
                   'MASTER_PORT           = 3306, '
                   'MASTER_CONNECT_RETRY  = 60, '
                   'MASTER_AUTO_POSITION  = 1, '
                   'MASTER_SSL            = 0; ',
                   (primary, node.mysql.repl_user, node.mysql.repl_password))
    node.mysql.add('START SLAVE;')
    node.mysql.execute_many()
