#!/usr/bin/python3

"""
Copyright (c) 2019-2022, Ian Santopietro
All rights reserved.

This file is part of RepoLib.

RepoLib is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

RepoLib is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with RepoLib.  If not, see <https://www.gnu.org/licenses/>.
"""

import atexit
import logging
import re
import tempfile

from enum import Enum
from pathlib import Path
from urllib.parse import urlparse
from urllib import request, error

import dbus

SOURCES_DIR = '/etc/apt/sources.list.d'
KEYS_DIR = '/usr/share/keyrings/'
TESTING = False
KEYSERVER_QUERY_URL = 'http://keyserver.ubuntu.com/pks/lookup?op=get&search=0x'

log = logging.getLogger(__name__)

class RepoError(Exception):
    """ Exception from this module."""

    def __init__(self, *args, code=1, **kwargs):
        """Exception with a source object

        Arguments:
            code (:obj:`int`, optional, default=1): Exception error code.
    """
        super().__init__(*args, **kwargs)
        self.code = code

try:
    import distro
    DISTRO_CODENAME = distro.codename()
except ImportError:
    DISTRO_CODENAME = 'linux'

class SourceFormat(Enum):
    """Enum of SourceFile Formats"""
    DEFAULT = "sources"
    LEGACY = "list"

class SourceType(Enum):
    """Enum of repository types"""
    BINARY = 'deb'
    SOURCECODE = 'deb-src'

    def ident(self) -> str:
        """Used for getting a version of the format for idents"""
        ident = f'{self.value}'
        ident = ident.replace('deb-src', 'source')
        ident = ident.replace('deb', 'binary')
        return ident

class AptSourceEnabled(Enum):
    """ Helper Enum to translate between bool data and the Deb822 format. """
    TRUE = 'yes'
    FALSE = 'no'

    def get_bool(self):
        """ Return a bool based on the value. """
        # pylint: disable=comparison-with-callable
        # This doesnt seem to actually be a callable in this case.
        if self.value == "yes":
            return True

        return False

valid_keys = [
    'X-Repolib-Name:',
    'X-Repolib-Ident:',
    'X-Repolib-Default-Mirror:',
    'X-Repolib-Comment',
    'Enabled:',
    'Types:',
    'URIs:',
    'Suites:',
    'Components:',
    'Architectures:',
    'Languages:',
    'Targets:',
    'PDiffs:',
    'By-Hash:',
    'Allow-Insecure:',
    'Allow-Weak:',
    'Allow-Downgrade-To-Insecure:',
    'Trusted:',
    'Signed-By:',
    'Check-Valid-Until:',
    'Valid-Until-Min:',
    'Valid-Until-Max:',
]

options_inmap = {
    'arch': 'Architectures',
    'lang': 'Languages',
    'target': 'Targets',
    'pdiffs': 'PDiffs',
    'by-hash': 'By-Hash',
    'allow-insecure': 'Allow-Insecure',
    'allow-weak': 'Allow-Weak',
    'allow-downgrade-to-insecure': 'Allow-Downgrade-To-Insecure',
    'trusted': 'Trusted',
    'signed-by': 'Signed-By',
    'check-valid-until': 'Check-Valid-Until',
    'valid-until-min': 'Valid-Until-Min',
    'valid-until-max': 'Valid-Until-Max'
}

options_outmap = {
    'Architectures': 'arch',
    'Languages': 'lang',
    'Targets': 'target',
    'PDiffs': 'pdiffs',
    'By-Hash': 'by-hash',
    'Allow-Insecure': 'allow-insecure',
    'Allow-Weak': 'allow-weak',
    'Allow-Downgrade-To-Insecure': 'allow-downgrade-to-insecure',
    'Trusted': 'trusted',
    'Signed-By': 'signed-by',
    'Check-Valid-Until': 'check-valid-until',
    'Valid-Until-Min': 'valid-until-min',
    'Valid-Until-Max': 'valid-until-max'
}

true_values = [
    True,
    'True',
    'true',
    'Yes',
    'yes',
    'YES',
    'y',
    'Y',
    AptSourceEnabled.TRUE,
    1
]

keys_map = {
    'X-Repolib-Name: ': 'Name: ',
    'X-Repolib-ID: ': 'Ident: ',
    'X-Repolib-Comments: ': 'Comments: ',
}

PRETTY_PRINT = '\n    '

_KEYS_TEMPDIR = tempfile.TemporaryDirectory()

options_re = re.compile(r'[^@.+]\[([^[]+.+)\]\ ')
uri_re = re.compile(r'\w+:(\/?\/?)[^\s]+')

CLEAN_CHARS = {
    33: None,
    64: 45,
    35: 45,
    36: 45,
    37: 45,
    94: 45,
    38: 45,
    42: 45,
    41: None,
    40: None,
    43: 45,
    61: 45,
    91: None,
    92: None,
    93: None,
    123: None,
    125: None,
    124: 95,
    63: None,
    47: 95,
    46: 45,
    60: 95,
    62: 95,
    44: 95,
    96: None,
    126: None,
    32: 95,
    58: None,
    59: None,
}

sources:dict = {}
files:dict = {}
keys:dict = {}
errors:dict = {}

def _cleanup_temsps() -> None:
    """Clean up our tempdir"""
    _KEYS_TEMPDIR.cleanup()

atexit.register(_cleanup_temsps)

def dbus_quit():
    bus = dbus.SystemBus()
    privileged_object = bus.get_object('org.pop_os.repolib', '/Repo')
    privileged_object.exit()

def compare_sources(source1, source2, excl_keys:list) -> bool:
    """Compare two sources based on arbitrary criteria.
    
    This looks at a given list of keys, and if the given keys between the two
    given sources are identical, returns True.

    Arguments:
        source1, source2(Source): The two sources to compare
        excl_keys([str]): Any keys to exclude from the comparison
    
    Returns: bool
        `True` if the sources are identical, otherwise `False`.
    """
    for key in source1:
        if key in excl_keys:
            continue
        if key in source2:
            if source1[key] != source2[key]:
                return False
            else:
                continue
        else:
            return False
    for key in source2:
        if key in excl_keys:
            continue
        if key in source1:
            if source1[key] != source2[key]:
                return False
            else:
                continue
        else:
            return False
    return True

def find_differences_sources(source1, source2, excl_keys:list) -> dict:
    """Find key-value pairs which differ between two sources.
    
    Arguments:
        source1, source2(Source): The two sources to compare
        excl_keys([str]): Any keys to exclude from the comparison
    
    Returns: dict{'key': ('source1[key]','source2[key]')}
        The dictionary of different keys, with the key values from each source.
    """
    differing_keys:dict = {}

    for key in source1:
        if key in excl_keys:
            continue
        if key in source2:
            if source1[key] == source2[key]:
                continue
            differing_keys[key] = (source1[key], source2[key])
        differing_keys[key] = (source1[key], '')
    for key in source2:
        if key in excl_keys:
            continue
        if key in source1:
            if source1[key] == source2[key]:
                continue
        differing_keys[key] = ('', source2[key])
    
    return differing_keys

def combine_sources(source1, source2) -> None:
    """Combine the data in two sources into one.
    
    Arguments:
        source1(Source): The source to be merged into
        source2(Source): The source to merge from
    """
    for key in source1:
        if key in ('X-Repolib-Name', 'X-Repolib-ID', 'Enabled', 'Types'):
            continue
        if key in source2:
            source1[key] += f' {source2[key]}'
    for key in source2:
        if key in ('X-Repolib-Name', 'X-Repolib-ID', 'Enabled', 'Types'):
            continue
        if key in source1:
            source1[key] += f' {source2[key]}'
    
    # Need to deduplicate the list
    for key in source1:
        vals = source1[key].strip().split()
        newvals = []
        for val in vals:
            if val not in newvals:
                newvals.append(val)
        source1[key] = ' '.join(newvals)
    for key in source2:
        vals = source2[key].strip().split()
        newvals = []
        for val in vals:
            if val not in newvals:
                newvals.append(val)
        source2[key] = ' '.join(newvals)


def prettyprint_enable(enabled: bool = True) -> None:
    """Easy helper to enable/disable pretty-printing for object reprs.
    
    Can also be used as an easy way to reset to defaults.

    Arguments:
        enabled(bool): Whether or not Pretty Printing should be enabled
    """
    global PRETTY_PRINT
    if enabled:
        PRETTY_PRINT = '\n    '
    else:
        PRETTY_PRINT = ''

def fetch_key(fingerprint, query_url=KEYSERVER_QUERY_URL):
    """ Fetches a PGP Key from a keyserver.

    Arguments:
        :str fingerprint: The fingerprint of the key to fetch.
        :str query_url: the URL to use to fetch the query.

    Returns
        :Bytes: The data containing the Key data.
    """

    full_url = query_url + fingerprint
    try:
        req = request.urlopen(full_url)
    except error.URLError:
        req = None

    return req.read()

def url_validator(url):
    """ Validate a url and tell if it's good or not.

    Arguments:
        url (str): The URL to validate.

    Returns:
        `True` if `url` is not malformed, otherwise `False`.
    """
    try:
        # pylint: disable=no-else-return,bare-except
        # A) We want to return false if the URL doesn't contain those parts
        # B) We need this to not throw any exceptions, regardless what they are
        result = urlparse(url)
        if not result.scheme:
            return False
        if result.scheme == 'x-repolib-name':
            return False
        if result.netloc:
            # We need at least a scheme and a netlocation/hostname or...
            return all([result.scheme, result.netloc])
        elif result.path:
            # ...a scheme and a path (this allows file:/// URIs which are valid)
            return all([result.scheme, result.path])
        return False
    except:
        return False

def get_source_path(name, log=None):
    """ Tries to get the full path to the source.

    This is necessary because some sources end in .list, others in .sources

    Returns:
        pathlib.Path for the actual full path.
    """
    full_name = f'{name}.sources'
    full_path = get_sources_dir() / full_name
    if log:
        log.debug('Trying to load %s', full_path)
    if full_path.exists():
        if log:
            log.debug('Path %s exists!', full_path)
        return full_path

    full_name = f'{name}.list'
    full_path = get_sources_dir() / full_name
    if log:
        log.debug('Trying to load %s', full_path)
    if full_path.exists():
        if log:
            log.debug('Path %s exists!', full_path)
        return full_path
    return None

def get_keys_dir(testing=False):
    """ Get the path to the signing keys dir.

    Arguments:
        :bool testing: Whether we should be in testing mode or not.

    Returns:
        pathlib.Path: The Keys dir.
    """
    # pylint: disable=global-statement
    # As with get_sources_dir(), we're setting a mode here.
    if testing:
        global KEYS_DIR
        KEYS_DIR = '/tmp/replib_testing/keys'
    # pylint: enable=global-statement
    keys_dir = Path(KEYS_DIR)
    keys_dir.mkdir(parents=True, exist_ok=True)
    return keys_dir

def get_sources_dir(testing=False):
    """ Get the path to the sources dir.

    Returns:
        pathlib.Path: The Sources dir.
    """
    # pylint: disable=global-statement
    # We want to stop using the old dir and use the testing dir on subsequent
    # calls.
    if testing:
        global SOURCES_DIR
        SOURCES_DIR = '/tmp/repolib_testing'
    # pylint: enable=global-statement
    sources_dir = Path(SOURCES_DIR)
    sources_dir.mkdir(parents=True, exist_ok=True)
    return sources_dir

# pylint: disable=inconsistent-return-statements
# This is a better way to check these
def validate_debline(valid):
    """ Basic checks to see if a given debline is valid or not.

    Arguments:
        valid (str): The line to validate.

    Returns:
        True if the line is valid, False otherwise.
    """
    if valid.startswith('#'):
        valid = valid.replace('#', '')
        valid = valid.strip()

    if valid.startswith("deb"):
        words = valid.split()
        for word in words:
            if url_validator(word):
                return True

    elif valid.startswith("ppa:"):
        if "/" in valid:
            return True

    else:
        if valid.endswith('.flatpakrepo'):
            return False
        if len(valid.split()) == 1:
            return url_validator(valid)
        return False

def strip_hashes(line:str) -> str:
    """ Strips the leading #'s from the given line.
    
    Arguments:
        line (str): The line to strip.
    
    Returns:
        (str): The input line without any leading/trailing hashes or 
            leading/trailing whitespace.
    """
    while True:
        line = line.strip('#')
        line = line.strip()
        if not line.startswith('#'):
            break
    
    return line
