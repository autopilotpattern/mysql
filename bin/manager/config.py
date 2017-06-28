""" autopilotpattern/mysql ContainerPilot configuraton wrapper """
import os
import signal
import subprocess

import json5

from manager.env import env, to_flag
from manager.utils import debug, log, UNASSIGNED


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
        Fetches the ContainerPilot config file and asks ContainerPilot
        to render it out so that all environment variables have been
        interpolated.
        """
        self.path = env('CONTAINERPILOT', None, envs)
        try:
            cfg = subprocess.check_output(['containerpilot', '-config',
                                           self.path, '-template'],
                                          env=envs.copy())
        except (subprocess.CalledProcessError, OSError) as ex:
            log.error('containerpilot -template returned error: %s', ex)
            raise(ex)

        config = json5.loads(cfg)
        self.config = config

    @debug(log_output=True)
    def update(self):
        """
        Update the on-disk config file if it is stale. Returns a
        bool indicating whether an update was made.
        """
        if self.state == UNASSIGNED:
            return False
        if self.state and self.config['jobs'][1]['name'] != self.state:
            self.config['jobs'][1]['name'] = self.state
            self._render()
            return True
        return False

    @debug
    def _render(self):
        """ Writes the current config to file. """
        new_config = json5.dumps(self.config)
        with open(self.path, 'w') as f:
            log.info('rewriting ContainerPilot config: %s', new_config)
            f.write(new_config)

    def reload(self):
        """ Force ContainerPilot to reload its configuration """
        log.info('Reloading ContainerPilot configuration.')
        try:
            subprocess.check_output(['containerpilot', '-reload'])
        except subprocess.CalledProcessError:
            log.info("call to 'containerpilot -reload' failed")
