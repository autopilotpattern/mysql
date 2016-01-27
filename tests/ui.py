from __future__ import print_function
import curses
import io
import os
import signal
import subprocess
from time import sleep
import tempfile
import traceback

#import pymysql
import consul as pyconsul
consul = pyconsul.Consul(host='192.168.99.100') #os.environ.get('CONSUL', 'consul'))

# tuple to define a border style where only the top border is in use
TOP_BORDER=(' ', ' ', 0, ' ', ' ', ' ', ' ', ' ')
DEBUG = os.environ.get('DEBUG', False)

class Pane(object):

    name = ''

    def __init__(self, screen, name='', y=0, nlines=5, ncols=80):
        self.screen = screen
        self.outer =  screen.subwin(nlines, ncols, y, 1)
        self.outer.border(*TOP_BORDER)
        if name:
            self.name = name
        self.outer.addstr(0, 0, self.name + ' ')
        self.subwin = self.outer.subwin(nlines - 2, ncols - 2, y + 2, 2)
        self.subwin.scrollok(True)
        self.count = 0

    def fetch(self):
        raise NotImplementedError()

    def paint(self):
        self.outer.refresh()
        last_fetch = self.fetch()
        self.subwin.addstr(0,0,last_fetch)
        if DEBUG:
            # This counter can be used to verify we're running during
            # testing & development if there aren't a lot of changes.
            self.count = self.count + 1
            self.subwin.addstr('{}'.format(self.count))

        self.subwin.refresh()

class DockerPane(Pane):

    name = 'Docker'

    def fetch(self):
        output = subprocess.check_output([
            'docker', 'ps',
            "--format='table {{.ID}}\\t{{.Names}}\\t{{.Command}}\\tUp {{.RunningFor}}'"])
        return output


class ConsulPane(Pane):

    name = 'Consul'

    def fetch(self):

        try:
            primary_node = consul.catalog.service('mysql-primary')[1]
            primary = dict(name=primary_node[0]['ServiceID'].replace('mysql-primary-', ''),
                           ip=primary_node[0]['Address'],
                           port=primary_node[0]['ServicePort'])

            replica_nodes = consul.catalog.service('mysql')[1]
            replicas = []
            for replica in replica_nodes:
                #raise Exception(repr(replica))
                replicas.append(dict(
                    name=replica['ServiceID'].replace('mysql-', ''),
                    ip=replica['Address'],
                    port=replica['ServicePort']
                ))

            lock_val = consul.kv.get('mysql-primary')[1]
            lock_session_id = lock_val['Session']
            lock_session = consul.session.info(lock_session_id)[1]
            lock = dict(id=lock_session_id,
                        hostname=lock_val['Value'],
                        ttl=lock_session['TTL'])

            last_backup = consul.kv.get('mysql-last-backup')[1]['Value']\
                                   .replace('mysql-backup-', '')
            last_binlog = consul.kv.get('mysql-last-binlog')[1]['Value']

            out = dict(primary=primary, replicas=replicas, lock=lock,
                       last_backup=last_backup, last_binlog=last_binlog)

            s = """Primary \t{primary[name]}\t{primary[ip]}:{primary[port]}\thealthy
Replicas\t{replicas[0][name]}\t{replicas[0][ip]}:{replicas[0][port]}\thealthy
\t\t{replicas[1][name]}\t{replicas[1][ip]}:{replicas[1][port]}\thealthy
Lock\t\t{lock[id]} by {lock[hostname]} with TTL {lock[ttl]}
Last Backup\t{last_backup}
Last Binlog\t{last_binlog}""".format(**out)
            return s
        except Exception as ex:
            return None


class LogsPane(Pane):
    """
    A LogsPane streams its data from a subprocess so the paint method is
    completely different and we need to make sure we clean up files and
    processes left over.
    """
    def fetch(self):
        try:
            fname = '/tmp/mysql-{}'.format(self.name)
            self.writer = io.open(fname, 'wb')
            self.reader = io.open(fname, 'rb', 1)
            self.proc = subprocess.Popen(['docker', 'logs', '-f',
                                          '--tail', '1', self.name],
                                         stdout=self.writer)
        except Exception as ex:
            self.writer.close()

    def __del__(self):
        try:
            if hasattr(self.proc) and self.proc:
                os.kill(self.proc.pid, signal.SIGTERM)
            if self.writer:
                self.writer.close()
        except OSError as ex:
            print(ex)

    def paint(self):
        if not hasattr(self, 'proc') or not self.proc:
            self.fetch()
        # TODO: right now we end up showing an error on screen when
        # the container isn't up yet.
        if self.proc and self.proc.poll() is None:
            self.subwin.addstr(self.reader.read())
            self.subwin.idlok(1)
            self.subwin.refresh()
            self.outer.refresh()

def main():
    try:
        screen = curses.initscr()
        maxy, maxx = screen.getmaxyx()
        curses.curs_set(0)
        screen.clear()
        screen.refresh()
        elements = (
            DockerPane(screen, y=0, nlines=9, ncols=maxx-1),
            ConsulPane(screen, y=9, nlines=8, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_1', y=18, nlines=8, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_2', y=27, nlines=8, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_3', y=35, nlines=8, ncols=maxx-1)
        )
        while True:
            for element in elements:
                element.paint()
    finally:
        curses.endwin()
        traceback.print_exc()



if __name__ == '__main__':
    main()
