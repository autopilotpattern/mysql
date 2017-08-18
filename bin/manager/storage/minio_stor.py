""" Module for storing snapshots in shared local disk """
import logging
import os
from shutil import copyfile

from manager.env import env, to_flag
from manager.utils import debug
from minio import Minio as pyminio, error as minioerror

logging.getLogger('manta').setLevel(logging.INFO)

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
        is_tls = env('MINIO_TLS_SECURE', False, envs, fn=to_flag)

        self.client = pyminio(
            self.url,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=is_tls)
        try:
            self.client.make_bucket(self.bucket, location=self.location)
        except minioerror.BucketAlreadyOwnedByYou:
            pass

    @debug
    def get_backup(self, backup_id):
        """
        Download file from Minio, allowing exceptions to bubble up.
        """
        try:
            os.mkdir('/tmp/backup', 0770)
        except OSError:
            pass
        outfile = '/tmp/backup/{}'.format(backup_id)
        self.client.fget_object(self.bucket, backup_id, outfile)

    def put_backup(self, backup_id, infile):
        """
        Upload the backup file to the expected path.
        """
        self.client.fput_object(self.bucket, backup_id, infile)
        return backup_id
