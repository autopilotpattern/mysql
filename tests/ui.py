from __future__ import print_function
import curses
import io
from itertools import cycle
import os
import signal
import subprocess
import traceback

import consul as pyconsul
from requests.exceptions import ConnectionError

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
        self.progress = cycle(['\\', '|', '/', '-', '\\', '|', '-'])

    def fetch(self):
        raise NotImplementedError()

    def paint(self):
        self.outer.refresh()
        last_fetch = self.fetch()
        self.subwin.addstr(0,0,last_fetch)
        if DEBUG:
            # This indicator can be used to verify we're running during
            # testing & development if there aren't a lot of changes.
            _, x = self.subwin.getmaxyx()
            self.subwin.addstr(0, x-2, '{}'.format(self.progress.next()))

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
            try:
                primary_node = consul.catalog.service('mysql-primary')[1]
                primary_name = primary_node[0]['ServiceID'].replace('mysql-primary-', '')
                primary_ip = primary_node[0]['Address']
                primary_port = primary_node[0]['ServicePort']
                out = 'Primary \t{}\t{}:{}\thealthy\n'.format(
                    primary_name, primary_ip, primary_port)
            except (IndexError, TypeError):
                out = 'Primary\n'

            # get information about the replicas and append it to the output
            try:
                replicas = []
                for replica in consul.catalog.service('mysql')[1]:
                    name = replica['ServiceID'].replace('mysql-', '')
                    ip = replica['Address']
                    port = replica['ServicePort']
                    replicas.append('\t{}\t{}:{}\thealthy'.format(name, ip, port))
                replicas_out = '\n\t'.join(replicas)
            except (IndexError, TypeError):
                replicas_out = ''
            out = out + 'Replicas' + replicas_out + '\n'

            # get information about the session lock we have for the primary
            try:
                lock_val = consul.kv.get('mysql-primary')[1]
                lock_host = lock_val['Value']
                lock_session_id = lock_val['Session']
                lock_ttl = consul.session.info(lock_session_id)[1]['TTL']
                lock = 'Lock\t\t{} by {} with TTL {}\n'.format(
                    lock_session_id, lock_host, lock_ttl)
            except (IndexError, TypeError):
                lock = 'Lock\n'
            out = out + lock

            try:
                last_backup = 'Last Backup\t{}\n'.format(
                    consul.kv.get('mysql-last-backup')[1]['Value']\
                    .replace('mysql-backup-', ''))
            except (IndexError, TypeError):
                last_backup = 'Last Backup\n'
            out = out + last_backup

            try:
                last_binlog = 'Last Binlog\t{}\n'.format(
                    consul.kv.get('mysql-last-binlog')[1]['Value'])
            except (IndexError, TypeError):
                last_binlog = 'Last Binlog'
            out = out + last_binlog

            return out
        except (ConnectionError, pyconsul.ConsulException):
            return ''
        except Exception as ex:
            return ex.message



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
                                         stdout=self.writer,
                                         stderr=self.writer)
        except Exception:
            raise
            self.proc = None
            self.writer.close()

    def __del__(self):
        try:
            if hasattr(self, 'proc') and self.proc:
                os.kill(self.proc.pid, signal.SIGTERM)
            if hasattr(self, 'writer') and self.writer:
                self.writer.close()
        except OSError as ex:
            print(ex)

    def paint(self):
        if not hasattr(self, 'proc') or not self.proc:
            self.fetch()
        if self.proc and self.proc.poll() is None:
            read = self.reader.read()
            self.subwin.addstr(read)
            self.subwin.idlok(1)
        else:
            self.proc = None
            self.subwin.clear()
            self.subwin.addstr(0, 0, 'No container {} running'.format(self.name))
            _, x = self.subwin.getmaxyx()
            self.subwin.addstr(0, x-2, self.progress.next())

        self.subwin.refresh()
        self.outer.refresh()


def main(*args, **kwargs):
    try:
        screen = curses.initscr()
        maxy, maxx = screen.getmaxyx()
        curses.curs_set(0)
        screen.clear()
        screen.refresh()
        elements = (
            DockerPane(screen, y=0, nlines=9, ncols=maxx-1),
            ConsulPane(screen, y=9, nlines=9, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_1', y=19, nlines=8, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_2', y=28, nlines=8, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_3', y=36, nlines=8, ncols=maxx-1)
        )
        while True:
            for element in elements:
                element.paint()
    finally:
        curses.endwin()
        traceback.print_exc()



if __name__ == '__main__':
    curses.wrapper(main)
