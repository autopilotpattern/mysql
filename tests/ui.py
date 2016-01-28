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
TOP_BORDER = (' ', ' ', 0, ' ', ' ', ' ', ' ', ' ')
DEBUG = os.environ.get('DEBUG', False)

class Pane(object):

    name = ''

    def __init__(self, screen, name='', y=0, nlines=5, ncols=80):
        self.screen = screen
        self.outer = screen.subwin(nlines, ncols, y, 1)
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
        self.subwin.clear()
        for y, line in enumerate(last_fetch.splitlines()):
            # hacky way of coloring the lines based on content
            if 'passing' in line:
                self.subwin.addstr(y, 0, line, curses.color_pair(curses.COLOR_GREEN))
            elif 'critical' in line:
                self.subwin.addstr(y, 0, line, curses.color_pair(curses.COLOR_RED))
            elif 'unknown' in line:
                self.subwin.addstr(y, 0, line, curses.color_pair(curses.COLOR_YELLOW))
            else:
                self.subwin.addstr(y, 0, line)
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
            out = self._fetch_primary()
            out = out + 'Replicas' + self._fetch_replicas() + '\n'
            out = out + self._fetch_lock()
            out = out + self._fetch_last_backup()
            out = out + self._fetch_last_binlog()
            return out
        except (ConnectionError, pyconsul.ConsulException):
            return ''
        except Exception as ex:
            return ex.message

    def _fetch_primary(self):
        try:
            primary_node = consul.catalog.service('mysql-primary')[1][0]
            primary_name = primary_node['ServiceID'].replace('mysql-primary-', '')
            primary_ip = primary_node['Address']
            primary_port = primary_node['ServicePort']
            try:
                primary_health = consul.health.checks('mysql-primary')[1][0]['Status']
            except (IndexError, TypeError):
                primary_health = 'unknown'

            out = 'Primary \t{}\t{}:{}\t\t{}\n'.format(
                primary_name, primary_ip, primary_port, primary_health)
        except (IndexError, TypeError):
            out = 'Primary\n'
        return out

    def _fetch_replicas(self):
        """ get information about the replicas and append it to the output """
        try:
            replica_nodes = consul.catalog.service('mysql')[1]
            replica_health = {n['Name']: n['Status']
                              for n in consul.health.checks('mysql')[1]}
            replicas = []
            for replica in replica_nodes:
                service_id = replica['ServiceID']
                name = service_id.replace('mysql-', '')
                ip = replica['Address']
                port = replica['ServicePort']
                health = replica_health.get(service_id, 'unknown')
                replicas.append('\t{}\t{}:{}\t\t{}'.format(name, ip, port, health))
            return '\n\t'.join(replicas)

        except IndexError:
            return ''

    def _fetch_lock(self):
        """ Get information about the session lock we have for the primary"""
        try:
            lock_val = consul.kv.get('mysql-primary')[1]
            lock_host = lock_val['Value']
            lock_session_id = lock_val['Session']
            lock_ttl = consul.session.info(lock_session_id)[1]['TTL']
            return '\nLock\t\t{} for TTL {} [ID:{}]\n'.format(
                lock_host, lock_ttl, lock_session_id)
        except (IndexError, TypeError):
            return 'Lock\n'

    def _fetch_last_backup(self):
        try:
            return 'Last Backup\t{}\n'.format(
                consul.kv.get('mysql-last-backup')[1]['Value']\
                .replace('mysql-backup-', ''))
        except (IndexError, TypeError):
            return 'Last Backup\n'

    def _fetch_last_binlog(self):
        try:
            return 'Last Binlog\t{}\n'.format(
                consul.kv.get('mysql-last-binlog')[1]['Value'])
        except (IndexError, TypeError):
            return 'Last Binlog'



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
        curses.start_color()
        curses.use_default_colors()
        for i in range(0, curses.COLORS):
            curses.init_pair(i, i, -1)
        _, maxx = screen.getmaxyx()
        curses.curs_set(0)
        screen.clear()
        screen.refresh()
        elements = (
            DockerPane(screen, y=0, nlines=9, ncols=maxx-1),
            ConsulPane(screen, y=9, nlines=10, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_1', y=20, nlines=8, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_2', y=29, nlines=8, ncols=maxx-1),
            LogsPane(screen, name='my_mysql_3', y=37, nlines=8, ncols=maxx-1)
        )
        while True:
            for element in elements:
                element.paint()
    finally:
        curses.endwin()
        traceback.print_exc()



if __name__ == '__main__':
    curses.wrapper(main)
