import os
from manager.utils import debug


class SnapshotBackup(object):
    """
    The SnapshotBackup class defines an expected interface to the
    backup storage, where we'll put our MySQL snapshots.
    """
    def __init__(self, envs=os.environ):
        raise NotImplementedError

    @debug
    def get_backup(self, backup_id):
        """
        fetch the snapshot file from the storage location, allowing
        exceptions to bubble up to the caller
        """
        raise NotImplementedError

    @debug
    def put_backup(self, backup_id, infile):
        """
        store the snapshot file to the expected path, allowing
        exceptions to bubble up to the caller.
        """
        raise NotImplementedError
