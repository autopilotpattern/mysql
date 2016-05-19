from __future__ import print_function
import datetime as dt
import json
import os
import subprocess
import time

import consul as pyconsul
from requests.exceptions import ConnectionError

from flask import Flask, render_template
app = Flask(__name__)


class Pane(object):

    def __init__(self, name=''):
        self.name = name
        self.last_timestamp = None

    def fetch(self):
        raise NotImplementedError()

class DockerPane(Pane):

    def fetch(self):
        """ Use the Docker client to run `docker ps` and returns a list """
        try:
            output = subprocess.check_output([
                'docker', 'ps',
                "--format='table {{.ID}};{{.Names}};{{.Command}};{{.RunningFor}}'"])
            out = []
        except subprocess.CalledProcessError:
            return {}

        for line in output.splitlines()[1:]:
            fields = line.split(';')
            out.append({
                "id": fields[0],
                "name": fields[1],
                "command": fields[2],
                "uptime": fields[3],
            })
        return out


class ConsulPane(Pane):

    _consul = None

    @property
    def consul(self):
        if self._consul:
            return self._consul

        docker = os.environ['DOCKER_HOST'] # let this crash if not defined
        docker = docker.replace('tcp://', '').replace(':2376','')
        if '192.' in docker:
            docker_host = docker
        else:
            try:
                output = subprocess.check_output([
                    'docker', 'inspect', 'my_consul_1'
                ])
                data = json.loads(output)
                docker_host = data[0]['NetworkSettings']['IPAddress']
            except (subprocess.CalledProcessError, KeyError):
                return None

        if docker_host:
            self._consul = pyconsul.Consul(host=docker_host)
        return self._consul


    def fetch(self):
        """
        Makes queries to Consul about the health of the mysql instances
        and status of locks/backups used for coordination.
        """
        try:
            if not self.consul:
                raise Exception("No Consul container running.")
            return {
                "primary": self._fetch_primary(),
                "replicas": self._fetch_replicas(),
                "lock": self._fetch_lock(),
                "last_backup": self._fetch_last_backup(),
                "last_binlog": self._fetch_last_binlog()
            }
        except (ConnectionError, pyconsul.ConsulException):
            return {"error": "Could not connect to Consul"}
        except Exception as ex:
            return {"error": ex.message}

    def _fetch_primary(self):
        """ Get status of the primary """
        try:
            primary_node = self.consul.catalog.service('mysql-primary')[1][0]
            primary_name = primary_node['ServiceID'].replace('mysql-primary-', '')
            primary_ip = primary_node['Address']
            primary_port = primary_node['ServicePort']
            try:
                primary_health = self.consul.health.checks('mysql-primary')[1][0]['Status']
            except (IndexError, TypeError):
                primary_health = 'unknown'

            return {
                "name": primary_name,
                "ip": primary_ip,
                "port": primary_port,
                "health": primary_health
            }

        except (IndexError, TypeError):
            out = {}
        return out

    def _fetch_replicas(self):
        """ Get status of the replicas """
        try:
            replica_nodes = self.consul.catalog.service('mysql')[1]
            replica_health = {n['Name']: n['Status']
                              for n in self.consul.health.checks('mysql')[1]}
            replicas = []
            for replica in replica_nodes:
                service_id = replica['ServiceID']
                name = service_id.replace('mysql-', '')
                ip = replica['Address']
                port = replica['ServicePort']
                health = replica_health.get(service_id, 'unknown')
                replicas.append({
                    "name": name,
                    "ip": ip,
                    "port": port,
                    "health": health
                })
            return replicas
        except IndexError:
            return []

    def _fetch_lock(self):
        """ Get information about the session lock we have for the primary"""
        try:
            lock_val = self.consul.kv.get('mysql-primary')[1]
            lock_host = lock_val['Value']
            lock_session_id = lock_val['Session']
            lock_ttl = self.consul.session.info(lock_session_id)[1]['TTL']
            return {
                "host": lock_host,
                "ttl": lock_ttl,
                "lock_session_id": lock_session_id
            }
        except:
            return {}

    def _fetch_last_backup(self):
        try:
            return self.consul.kv.get('mysql-last-backup')[1]['Value']\
                            .replace('mysql-backup-', '')
        except (IndexError, TypeError):
            return ''

    def _fetch_last_binlog(self):
        try:
            return self.consul.kv.get('mysql-last-binlog')[1]['Value']
        except (IndexError, TypeError):
            return ''


class LogsPane(Pane):
    """
    A LogsPane streams its data from a subprocess so the paint method is
    completely different and we need to make sure we clean up files and
    processes left over.
    """
    def fetch(self):

        args = ['docker', 'logs', '-t']
        if self.last_timestamp:
            args.extend(['--since', self.last_timestamp])
        args.append(self.name)

        try:
            output = subprocess.check_output(args, stderr=subprocess.STDOUT).splitlines()
        except subprocess.CalledProcessError:
            return []

        if len(output) > 0:
            # we get back the UTC timestamp from the server but the client
            # uses the local TZ for the timestamp for some unholy reason,
            # so we need to add an offset
            ts = output[-1].split()[0]
            utc_dt = dt.datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
            now = time.time()
            offset = dt.datetime.fromtimestamp(now) - dt.datetime.utcfromtimestamp(now)
            utc_dt = utc_dt + offset
            self.last_timestamp = str(int(time.mktime(utc_dt.timetuple())))

        # trim out the `docker logs` timestamps b/c we have the timestamps
        # from the servers
        output = [line[31:] for line in output]
        return output

@app.template_filter('colorize')
def colorize(value):
    if value == 'critical':
        return '<span style="color:#c52717">critical</span>'
    elif value == 'passing':
        return '<span style="color:#00ff00">passing</span>'
    else:
        return value


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/docker')
def docker_handler():
    resp = docker_pane.fetch()
    return render_template('docker.html', docker=resp)

@app.route('/consul')
def consul_handler():
    resp = consul_pane.fetch()
    return render_template('consul.html', consul=resp)

@app.route('/mysql/<int:mysql_id>')
def mysql_handler(mysql_id):
    resp = mysql_panes[mysql_id - 1].fetch()
    return render_template('mysql.html', logs=resp)


if __name__ == '__main__':
    docker_pane = DockerPane(name='Docker')
    consul_pane = ConsulPane(name='Consul')
    mysql_panes = [
        LogsPane(name='my_mysql_1'),
        LogsPane(name='my_mysql_2'),
        LogsPane(name='my_mysql_3'),
    ]

    app.run(threaded=True, debug=True)
