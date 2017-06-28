""" utility functions """
from functools import wraps
import logging
import os
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
