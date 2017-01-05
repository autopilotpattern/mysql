""" utility functions """
import fcntl
from functools import wraps
import logging
import os
import socket
import struct
import sys

# pylint: disable=invalid-name,no-self-use,dangerous-default-value

# ---------------------------------------------------------
# common consts

PRIMARY = 'mysql-primary'
REPLICA = 'mysql'
UNASSIGNED = 'UNASSIGNED'

# ---------------------------------------------------------
# logging setup

logging.basicConfig(format='%(levelname)s manage %(message)s',
                    stream=sys.stdout,
                    level=logging.getLevelName(
                        os.environ.get('LOG_LEVEL', 'INFO')))
log = logging.getLogger()

# reduce noise from requests logger
logging.getLogger('requests').setLevel(logging.WARN)


# ---------------------------------------------------------
# errors and debugging setup

class WaitTimeoutError(Exception):
    """ Exception raised when a timeout occurs. """
    pass

class UnknownPrimary(Exception):
    """ Exception raised when we can't figure out which node is primary """
    pass

def debug(fn=None, log_output=False):
    """
    Function/method decorator to trace calls via debug logging. Acts as
    pass-thru if not at LOG_LEVEL=DEBUG. Normally this would kill perf but
    this application doesn't have significant throughput.
    """
    def _decorate(fn, *args, **kwargs):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                # because we have concurrent processes running we want
                # to tag each stack with an identifier for that process
                msg = "[{}]".format(sys.argv[1])
            except IndexError:
                msg = "[pre_start]"
            if len(args) > 0:
                cls_name = args[0].__class__.__name__.lower()
                name = '{}.{}'.format(cls_name, fn.__name__)
            else:
                name = fn.__name__
            log.debug('%s %s start', msg, name)
            out = apply(fn, args, kwargs)
            if log_output: # useful for checking status flags
                log.debug('%s %s end: %s', msg, name, out)
            else:
                log.debug('%s %s end', msg, name)
            return out
        return wrapper
    if fn:
        return _decorate(fn)
    return _decorate


# ---------------------------------------------------------
# misc utility functions for setting up environment

def env(key, default, environ=os.environ, fn=None):
    """
    Gets an environment variable, trims away comments and whitespace,
    and expands other environment variables.
    """
    val = environ.get(key, default)
    try:
        val = val.split('#')[0]
        val = val.strip()
        val = os.path.expandvars(val)
    except (AttributeError, IndexError):
        # just swallow AttributeErrors for non-strings
        pass
    if fn: # transformation function
        val = fn(val)
    return val

def to_flag(val):
    """
    Parse environment variable strings like "yes/no", "on/off",
    "true/false", "1/0" into a bool.
    """
    try:
        return bool(int(val))
    except ValueError:
        val = val.lower()
        if val in ('false', 'off', 'no', 'n'):
            return False
            # non-"1" or "0" string, we'll treat as truthy
        return bool(val)


# env values for keys
PRIMARY_KEY = env('PRIMARY_KEY', env('SERVICE_NAME','mysql')+'-primary')
LAST_BACKUP_KEY = env('LAST_BACKUP_KEY', 'mysql-last-backup')
BACKUP_LOCK_KEY = env('BACKUP_LOCK_KEY', 'mysql-backup-running')
LAST_BINLOG_KEY = env('LAST_BINLOG_KEY', 'mysql-last-binlog')
BACKUP_NAME = env('BACKUP_NAME', 'mysql-backup-%Y-%m-%dT%H-%M-%SZ')
BACKUP_TTL = env('BACKUP_TTL', 86400, fn='{}s'.format) # every 24 hours

def get_ip(iface='eth0'):
    """
    Use Linux SIOCGIFADDR ioctl to get the IP for the interface.
    ref http://code.activestate.com/recipes/439094-get-the-ip-address\
        -associated-with-a-network-inter/
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    return socket.inet_ntoa(fcntl.ioctl(
        sock.fileno(),
        0x8915, # SIOCGIFADDR
        struct.pack('256s', iface[:15])
    )[20:24])
