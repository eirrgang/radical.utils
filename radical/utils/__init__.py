
__author__    = "Radical.Utils Development Team (Andre Merzky, Ole Weidner)"
__copyright__ = "Copyright 2013, RADICAL@Rutgers"
__license__   = "MIT"


import os

version=open (os.path.dirname (os.path.abspath (__file__)) + "/VERSION", 'r').read().strip()


# import utility classes
from object_cache   import ObjectCache
from plugin_manager import PluginManager
from singleton      import Singleton
from threads        import Thread, RLock, NEW, RUNNING, DONE, FAILED
from url            import Url
from dict_mixin     import DictMixin, dict_merge, dict_stringexpand
from lockable       import Lockable
from registry       import Registry, READONLY, READWRITE
from regex          import ReString, ReSult
from reporter       import Reporter

# import utility methods
from ids            import generate_id, ID_SIMPLE, ID_UNIQUE
from read_json      import read_json
from read_json      import read_json_str
from read_json      import parse_json
from read_json      import parse_json_str
from tracer         import trace, untrace
from which          import which
from misc           import split_dburl

# import sub-modules
# from config         import Configuration, Configurable, ConfigOption, getConfig


# ------------------------------------------------------------------------------

