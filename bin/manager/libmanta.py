""" Module for Manta client wrapper and related tooling. """
import logging
import os
from manager.utils import debug, env, to_flag

# pylint: disable=import-error,dangerous-default-value,invalid-name
import manta as pymanta

# Manta client barfing if we log the body of binary data
logging.getLogger('manta').setLevel(logging.INFO)

class Manta(object):
    """
    The Manta class wraps access to the Manta object store, where we'll put
    our MySQL backups.
    """
    def __init__(self, envs=os.environ):
        self.account = env('MANTA_USER', None, envs)
        self.user = env('MANTA_SUBUSER', None, envs)
        self.role = env('MANTA_ROLE', None, envs)
        self.key_id = env('MANTA_KEY_ID', None, envs)
        self.url = env('MANTA_URL', 'https://us-east.manta.joyent.com', envs)
        self.bucket = env('MANTA_BUCKET', '/{}/stor'.format(self.account), envs)
        is_tls = env('MANTA_TLS_INSECURE', False, envs, fn=to_flag)

        # we don't want to use `env` here because we have a different
        # de-munging to do
        self.private_key = envs.get('MANTA_PRIVATE_KEY', '').replace('#', '\n')
        self.signer = pymanta.PrivateKeySigner(self.key_id, self.private_key)
        self.client = pymanta.MantaClient(
            self.url,
            self.account,
            subuser=self.user,
            role=self.role,
            disable_ssl_certificate_validation=is_tls,
            signer=self.signer)

    @debug
    def get_backup(self, backup_id):
        """ Download file from Manta, allowing exceptions to bubble up """
        try:
            os.mkdir('/tmp/backup', 0770)
        except OSError:
            pass
        outfile = '/tmp/backup/{}'.format(backup_id)
        mpath = '{}/{}'.format(self.bucket, backup_id)
        data = self.client.get_object(mpath)
        with open(outfile, 'w') as f:
            f.write(data)

    def put_backup(self, backup_id, infile):
        """ Upload the backup file to the expected path """
        # TODO: stream this backup once python-manta supports it:
        # ref https://github.com/joyent/python-manta/issues/6
        mpath = '{}/{}'.format(self.bucket, backup_id)
        with open(infile, 'r') as f:
            self.client.put_object(mpath, file=f)
