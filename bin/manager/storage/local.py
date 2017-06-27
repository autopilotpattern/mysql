""" Module for storing snapshots in shared local disk """
import os
from shutil import copyfile

from manager.env import env
from manager.utils import debug

class Local(object):
    """

    The Manta class wraps access to the Manta object store, where we'll put
    our MySQL backups.
    """
    def __init__(self, envs=os.environ):
        self.dir = env('STORAGE_DIR', '/tmp/snapshots', envs)

    @debug
    def get_backup(self, backup_id):
        """
        copies snapshot from 'STORAGE_DIR' location to a working
        directory so it can be loaded into the DB without worrying
        about other processes writing to the snapshot.
        """
        try:
            os.mkdir(self.dir, 0770)
        except OSError:
            pass
        try:
            os.mkdir('/tmp/backup', 0770)
        except OSError:
            pass

        dst = '/tmp/backup/{}'.format(backup_id)
        src = '{}/{}'.format(self.dir, backup_id)
        copyfile(src, dst)

    def put_backup(self, backup_id, src):
        """
        copies snapshot to 'STORAGE_DIR'
        """
        dst = '{}/{}'.format(self.dir, backup_id)
        copyfile(src, dst)
        return dst
