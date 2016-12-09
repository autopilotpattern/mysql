""" autopilotpattern/mysql ContainerPilot configuraton wrapper """
import json
import os
import signal
from manager.utils import debug, env, to_flag, log, UNASSIGNED

# pylint: disable=invalid-name,no-self-use,dangerous-default-value

class ContainerPilot(object):
    """
    ContainerPilot config is where we rewrite ContainerPilot's own config
    so that we can dynamically alter what service we advertise.
    """

    def __init__(self):
        self.state = UNASSIGNED
        self.path = None
        self.config = None

    def load(self, envs=os.environ):
        """
        Parses the ContainerPilot config file and interpolates the
        environment into it the same way that ContainerPilot does.
        The `state` attribute will be populated with the state
        derived from the configuration file and not the state known
        by Consul.
        """
        self.path = env('CONTAINERPILOT', None, envs,
                        lambda x: x.replace('file://', ''))
        with open(self.path, 'r') as f:
            cfg = f.read()

        # remove templating so that we can parse it as JSON; we'll
        # override the attributes directly in the resulting dict
        cfg = cfg.replace('[{{ if .CONSUL_AGENT }}', '[')
        cfg = cfg.replace('}{{ end }}', '}')
        config = json.loads(cfg)

        if env('CONSUL_AGENT', False, envs, to_flag):
            config['consul'] = 'localhost:8500'
            cmd = config['coprocesses'][0]['command']
            host_cfg_idx = cmd.index('-retry-join') + 1
            cmd[host_cfg_idx] = env('CONSUL', 'consul', envs)
            config['coprocesses'][0]['command'] = cmd
        else:
            config['consul'] = env('CONSUL', 'consul', envs,
                                   fn='{}:8500'.format)
            config['coprocesses'] = []

        self.config = config

    @debug(log_output=True)
    def update(self):
        """
        Update the on-disk config file if it is stale. Returns a
        bool indicating whether an update was made.
        """
        if self.state == UNASSIGNED:
            return False
        if self.state and self.config['services'][0]['name'] != self.state:
            self.config['services'][0]['name'] = self.state
            self._render()
            return True
        return False

    @debug
    def _render(self):
        """ Writes the current config to file. """
        new_config = json.dumps(self.config)
        with open(self.path, 'w') as f:
            log.info('rewriting ContainerPilot config: %s', new_config)
            f.write(new_config)

    def reload(self):
        """ Force ContainerPilot to reload its configuration """
        log.info('Reloading ContainerPilot configuration.')
        os.kill(1, signal.SIGHUP)
