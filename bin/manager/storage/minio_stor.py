""" Module for storing snapshots in shared local disk """
import os
from shutil import copyfile

from manager.env import env
from manager.utils import debug
from minio import Minio as pyminio

class Minio(object):
    """

    The Minio class wraps access to the Minio object store, where we'll put
    our MySQL backups.
    """
    def __init__(self, envs=os.environ):
        self.access_key = env('MINIO_ACCESS_KEY', None, envs)
        self.secret_key = env('MINIO_SECRET_KEY', None, envs)
        self.bucket = env('MINIO_BUCKET', 'backups', envs)
        self.location = env('MINIO_LOCATION', 'us-east-1', envs)
        self.url = env('MINIO_URL', 'minio:9000')
        is_tls - env('MINIO_TLS_INSECURE', False, envs, fn=to_flag)

        self.client = pyminio(
            self.url,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=is_tls)

        try:
            self.client.make_bucket(self.bucket, location=self.location)
        except:
            raise

    @debug
    def get_backup(self, backup_id):
        """
        Download file from Manta, allowing exceptions to bubble up.
        """
        return NotImplementedError

    def put_backup(self, backup_id, src):
        """
        Upload the backup file to the expected path.
        """
        return NotImplementedError
