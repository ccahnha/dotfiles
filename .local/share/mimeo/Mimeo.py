#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (C) 2009-2016  Xyne
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# (version 2) as published by the Free Software Foundation.
#
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

'''
Mimeo *should* follow the latest version of the specifications found on the
freedesktop.org site:

    http://standards.freedesktop.org/mime-apps-spec/mime-apps-spec-latest.html
    http://standards.freedesktop.org/shared-mime-info-spec/shared-mime-info-spec-latest.html
    http://standards.freedesktop.org/basedir-spec/basedir-spec-latest.html
    http://standards.freedesktop.org/desktop-entry-spec/desktop-entry-spec-latest.html
    http://standards.freedesktop.org/icon-theme-spec/icon-theme-spec-latest.html

Internally Mimeo uses pyxdg:

    http://freedesktop.org/wiki/Software/pyxdg/
    http://pyxdg.readthedocs.org/en/latest/index.html

$XDG_DATA_HOME/applications is still supported for custom Desktop files even if
the mimeapps.list file is no longer supported in that directory:

    https://specifications.freedesktop.org/menu-spec/menu-spec-latest.html#adding-items

'''

import argparse
import collections
import fnmatch
import glob
import itertools
import logging
import mimetypes
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import urllib.parse

import xdg.BaseDirectory
import xdg.DesktopEntry
import xdg.Mime


##################################### TODO #####################################
'''
* Consider ways to generalize all of the pair iterations and collections.
* Maybe create a MimeappsList class to encapsulate association modifications.
'''



################################### Globals ####################################

NAME = 'Mimeo'
MIMEO_DEFAULT_ARGUMENTS_FILE = 'default_arguments.txt'
MIMEO_ASSOCIATIONS_FILE = 'associations.txt'

# Files and paths
MIMEAPPS_LIST_FILE = 'mimeapps.list'
MIMEINFO_CACHE_FILE = 'mimeinfo.cache'
APP_DIR = 'applications'
DEFAULTS_LIST_FILE = 'defaults.list'
DESKTOP_EXTENSION = '.desktop'

# Name of current desktop for desktop-specific configuration.
XDG_CURRENT_DESKTOP = 'XDG_CURRENT_DESKTOP'

# File sections
ADDED_ASSOCIATIONS_SECTION = 'Added Associations'
REMOVED_ASSOCIATIONS_SECTION = 'Removed Associations'
DEFAULT_APPLICATIONS_SECTION = 'Default Applications'
MIME_CACHE_SECTION = 'MIME Cache'


ASSOCIATION_ADDERS = {
  'add'    : (ADDED_ASSOCIATIONS_SECTION,),
  'remove' : (REMOVED_ASSOCIATIONS_SECTION,),
  'prefer' : (DEFAULT_APPLICATIONS_SECTION,),
}
ASSOCIATION_REMOVERS = {
  'unadd'    : (ADDED_ASSOCIATIONS_SECTION,),
  'unremove' : (REMOVED_ASSOCIATIONS_SECTION,),
  'unprefer' : (DEFAULT_APPLICATIONS_SECTION,),
  'clear'    : (
                ADDED_ASSOCIATIONS_SECTION,
                REMOVED_ASSOCIATIONS_SECTION,
                DEFAULT_APPLICATIONS_SECTION
              )
}

# Executables
EXE_UPDATE_DESKTOP_DATABASE = 'update-desktop-database'
# The command-line utility, for another way to determine MIME-types.
EXE_FILE = 'file'

# URL scheme
SCHEME_FILE = 'file'

# MIME-types
MIMETYPE_SCHEME_FMT = 'x-scheme-handler/{}'

# http://standards.freedesktop.org/shared-mime-info-spec/shared-mime-info-spec-latest.html#idm140625828597376
MIMETYPE_BLOCKDEVICE= 'inode/blockdevice'
MIMETYPE_CHARDEVICE = 'inode/chardevice'
MIMETYPE_DIRECTORY = 'inode/directory'
MIMETYPE_FIFO = 'inode/fifo'
MIMETYPE_MOUNT_POINT = 'inode/mount-point'
MIMETYPE_SOCKET = 'inode/socket'
MIMETYPE_SYMLINK = 'inode/symlink'

# Desktop files
EXEC_RESERVED = ' \t\n"\'\\><~|&;$*?#()`'
EXEC_ESCAPED = '"`$\\'

# MIME-type matching
MATCHER_PREFIX_GLOB = 'glob:'
MATCHER_PREFIX_REGEX = 'regex:'

MIMETYPES_KNOWNFILES_REGEX = re.compile('^\s*([^#\s]\S+\/\S+)')

TERM_COMMAND_PLACEHOLDER = '%s'

ASSOCIATION_MODIFICATION_METAVAR = ('<MIME-type matcher | filepath | desktop file>', '<desktop file>')



############################### Config Functions ###############################

def default_mimeo_associations_paths():
  '''
  Paths to check for custom Mimeo associations.
  '''
  for dpath in xdg.BaseDirectory.xdg_config_dirs:
    yield os.path.join(
      dpath,
      NAME.lower(),
      MIMEO_ASSOCIATIONS_FILE
    )



def default_arguments_path():
  '''
  The path to a plaintext file containing shell-parsable arguments to add to
  Mimeo before argument parsing.
  '''
  return os.path.join(
    xdg.BaseDirectory.xdg_config_home,
    NAME.lower(),
    MIMEO_DEFAULT_ARGUMENTS_FILE
  )



def default_arguments():
  '''
  Load default arguments from default_arguments_path().
  '''
  path = default_arguments_path()
  logging.debug('loading arguments from {}'.format(path))
  try:
    with open(path, 'r') as f:
      return shlex.split(f.readline())
  except FileNotFoundError:
    return None



################################## Debugging ###################################

def logging_debug_and_yield(msg, lst):
  '''
  Pretty-print a debugging message followed by a list of arguments. This is an
  iterator so that it can be used to log lists with "yield from" without
  building an intermediate list or tuple.
  '''
  for item in lst:
    logging.debug('{}: {}'.format(msg, item))
    yield item



############################## Generic Functions ###############################

def quote_cmd(cmd):
  '''
  Quote a command for shell parsing (used for command-line output).
  '''
  return ' '.join(shlex.quote(w) for w in cmd)



def run_cmd(cmd, quiet=False):
  '''
  Start a command without waiting for it to finish.
  '''
  if quiet:
    kwargs = {
      'stdout' : subprocess.DEVNULL,
      'stderr' : subprocess.DEVNULL,
    }
  else:
    kwargs = dict()
  logging.debug(quote_cmd(cmd))
  subprocess.Popen(cmd, close_fds=True, **kwargs)


def interpolate_term_cmd(term_cmd, app_cmd):
  '''
  Interpolate a terminal command, given as a single string, with the given
  application command.
  '''
  seen = False
  app_cmd_word = ' '.join(shlex.quote(w) for w in app_cmd)
  for word in shlex.split(term_cmd):
    if word == TERM_COMMAND_PLACEHOLDER:
      seen = True
      yield from app_cmd
    elif word == '\'{}\''.format(TERM_COMMAND_PLACEHOLDER):
      seen = True
      yield app_cmd_word
    else:
      iword = ''
      escaped = False
      for c in word:
        if escaped:
          if c == TERM_COMMAND_PLACEHOLDER[1]:
            iword += app_cmd_word
          else:
            iword += c
          escaped = False
        elif c == TERM_COMMAND_PLACEHOLDER[0]:
          escaped = True
          continue
        else:
          iword += c
      yield iword



def which(cmd):
  '''
  Emulate the system command "which".
  '''
  if not cmd:
    return None
  elif os.path.isabs(cmd):
    return cmd
  else:
    for p in os.get_exec_path():
      fpath = os.path.join(p, cmd)
      logging.debug('which: {}'.format(fpath))
      if os.path.isfile(fpath) and os.access(fpath, os.X_OK):
        return fpath
    else:
      return None



def unique_items(f):
  '''
  Function decorator to remove duplicates from iterable functions.
  '''
  def g(*args, **kwargs):
    seen = set()
    for x in f(*args, **kwargs):
      if x in seen:
        continue
      else:
        yield x
        seen.add(x)
  return g



def ensure_url(arg):
  '''
  Ensure that the argument is a URL. If not, it is assumed to be a file path and
  adapted to a file:// URL.
  '''
  parsed_url = urllib.parse.urlparse(arg)
  if parsed_url.scheme:
    return parsed_url.geturl()
  else:
    return urllib.parse.urlunsplit((
      SCHEME_FILE,
      None,
      urllib.parse.quote(os.path.abspath(arg)),
      None,
      None
    ))



def ensure_path(arg):
  '''
  Ensure that the argument is a path. If it is a URL, only the path part will
  be returned.
  '''
  parsed_url = urllib.parse.urlparse(arg)
  # Not a URL. Return the argument directly.
  if not (parsed_url.scheme or parsed_url.netloc):
    return arg

  # "file" URL on localhost
  if parsed_url.scheme == SCHEME_FILE:
    # Keep this here to avoid getfqdn calls for non-"file" URLs, which have been
    # reported to be slow on some systems.
    localhost = socket.getfqdn(socket.gethostname())
    hostname = parsed_url.hostname if parsed_url.hostname else 'localhost'
    remotehost = socket.getfqdn(hostname)
    if remotehost == localhost:
      return urllib.parse.unquote(parsed_url.path)

  return None



def ensure_desktop_names(args):
  '''
  Add the desktop extension if it is missing.
  '''
  for a in args:
    a = os.path.basename(a)
    yield a if a.endswith(DESKTOP_EXTENSION) else a + DESKTOP_EXTENSION



def swap_a_and_b(itr):
  '''
  Swap the items in a pair iterator.
  '''
  for a, b in itr:
    yield b, a



def apply_func(itr, fa=None, fb=None):
  '''
  Apply optional functions to the elements of 2-tuples in an iterator.
  '''
  for a, b in itr:
    if fa and a is not None:
      a = fa(a)
    if fb and b is not None:
      b = fb(b)
    yield a, b



def collect_b_by_a(itr, unique_b=True, preserve_order=True):
  '''
  Iterate over a list of 2-tuples and accumulate the second item into a
  dictionary with the first item as the key.
  '''
  if preserve_order:
    b_by_a = collections.OrderedDict()
  else:
    b_by_a = dict()
  for a, b in itr:
    try:
      if not unique_b or b not in b_by_a[a]:
        b_by_a[a].append(b)
    except KeyError:
      b_by_a[a] = [b]
  return b_by_a



def modify_and_collect(a_to_b, fa=None, fb=None, swap=False):
  '''
  Wrapper function for optionally applying functions and swapping before
  collecting.
  '''
  if fa or fb:
    a_to_b = apply_func(a_to_b, fa=fa, fb=fb)
  if swap:
    a_to_b = swap_a_and_b(a_to_b)
  return collect_b_by_a(a_to_b)



# TODO
# Maybe add optional color output.
def print_collection(a_by_b, order=None, sort_a=False, sort_b=False):
  '''
  Print a collection to STDOUT.
  '''
  if not order:
    if sort_a:
      try:
        order = sorted(a_by_b)
      except TypeError:
        order = sorted(b for b in a_by_b if b is not None) + [None]
    else:
      order = a_by_b.keys()

  for a in order:
    print(a)
    try:
      bs = a_by_b[a]
    except KeyError:
      continue
    else:
      if sort_b:
        bs = sorted(bs)
      for b in bs:
        print('  {}'.format(b))



########################### Mimeo Associations File ############################

def parse_mimeo_associations(fpath):
  cmd = None
  logging.debug('checking {}'.format(fpath))
  try:
    with open(fpath, 'r') as f:
      logging.debug('loading {}'.format(fpath))
      for line in f:
        line = line.rstrip()
        if not line or line[0] == '#':
          continue
        elif line.startswith('  '):
          if cmd:
            regex = re.compile(line[2:])
            yield regex, cmd
        else:
          cmd = line
  except FileNotFoundError:
    logging.debug('{} does not exist'.format(fpath))



def args_to_custom_cmds(mimeo_assocs, args, at_least_one=False, first_only=False):
  '''
  Collect arguments by matching command.
  '''
  for a in args:
    found_one = False
    for regex, cmd in mimeo_assocs:
      if regex.search(a):
        yield a, cmd
        if first_only:
          break
        found_one = True
    if not found_one:
      if at_least_one:
        yield None



################################## MIME-types ##################################

def parse_mimetype(mimetype):
  '''
  Parse a MIME-type string into the following components, returned as a tuple:

  * top-level type name
  * tree or None
  * subtype name
  * suffix or None
  * parameters or None
  '''
  type_name, rest = mimetype.split('/', 1)
  try:
    tree, rest = rest.split('.', 1)
  except ValueError:
    tree = None
  try:
    rest, parameters = rest.rsplit(';', 1)
  except ValueError:
    parameters = None
  try:
    subtype_name, suffix = rest.split('+', 1)
  except ValueError:
    subtype_name = rest
    suffix = None
  return type_name, tree, subtype_name, suffix, parameters



def strip_mimetype(mimetype):
  '''
  Strip all components (tree, suffix and parameters) and return just the
  top-level type name and subtype name.
  '''
  type_name, _, subtype_name, _, _ = parse_mimetype(mimetype)
  return '{}/{}'.format(type_name, subtype_name)



@unique_items
def mimetypes_from_path(arg, follow_symlinks=True, content_first=True, content_only=False, name_only=False):
  '''
  Attempt to determine the MIME-type of the argument.
  '''
  try:
    if follow_symlinks:
      st = os.stat(arg)
    else:
      st = os.lstat(arg)
  except FileNotFoundError:
    mimetype = file_mimetype_by_name(arg)
    if mimetype:
      yield mimetype
  except PermissionError as e:
    logging.error('mimetypes_from_path: [{}]'.format(e))
    mimetype = file_mimetype_by_name(arg)
    if mimetype:
      yield mimetype
  else:
    mode = st.st_mode
    if stat.S_ISBLK(mode):
      yield MIMETYPE_BLOCKDEVICE
    elif stat.S_ISCHR(mode):
      yield MIMETYPE_CHARDEVICE
    elif stat.S_ISDIR(mode):
      yield MIMETYPE_DIRECTORY
    elif stat.S_ISFIFO(mode):
      yield MIMETYPE_FIFO
    elif stat.S_ISSOCK(mode):
      yield MIMETYPE_SOCKET
    elif stat.S_ISLNK(mode):
      yield MIMETYPE_SYMLINK
    elif stat.S_ISREG(mode):
      for m in file_mimetype(
        arg,
        content_first=content_first,
        content_only=content_only,
        name_only=name_only,
      ):
        yield m
    else:
      logging.error('mimetypes_from_path: unsupported mode [{}]'.format(mode))



@unique_items
def file_mimetype(path, content_first=True, content_only=False, name_only=False):
  '''
  Attempt to determine the MIME-type of a regular (existing) file.
  '''
  rpath = os.path.realpath(path)
  if content_only:
    fs = (file_mimetype_by_content,)
  elif name_only:
    fs = (file_mimetype_by_name,)
  elif content_first:
    fs = (file_mimetype_by_content, file_mimetype_by_name)
  else:
    fs = (file_mimetype_by_name, file_mimetype_by_content)
  for f in fs:
    try:
      mimetype = f(path)
    except FileNotFoundError:
      logging.warning('file not found: {}'.format(path))
    else:
      if mimetype:
        yield mimetype



def file_mimetype_by_content(path):
  '''
  Attempt to determine the MIME-type of a regular (existing) file by content.
  '''
  mimetype = None
  mt = xdg.Mime.get_type_by_contents(path)
  if mt:
    mimetype = '{}/{}'.format(mt.media, mt.subtype)
  if not mimetype:
    cmd = [EXE_FILE, '--mime-type', path]
    cp = subprocess.run(cmd, stdout=subprocess.PIPE, check=True)
    mimetype = cp.stdout.rsplit(b': ', 1)[-1].strip().decode()
  return mimetype



def file_mimetype_by_name(path):
  '''
  Attempt to determine the MIME-type of a regular (existing) file by name.
  '''
  mimetype = None
  mt = xdg.Mime.get_type_by_name(path)
  if mt:
    mimetype = '{}/{}'.format(mt.media, mt.subtype)
  if not mimetype:
    mimetype = mimetypes.guess_type(path)[0]
  return mimetype



def mimetype_regex(matcher):
  '''
  Convert a MIME-type matcher to a regular expression. The following are
  supported:

  * <MATCHER_PREFIX_GLOB><pattern>  shell-style globbing pattern
  * <MATCHER_PREFIX_REGEX><pattern> Python regular expression
  * <string>                        plain string to match
  '''
  pattern = False
  if matcher.startswith(MATCHER_PREFIX_GLOB):
    p = fnmatch.translate(matcher[len(MATCHER_PREFIX_GLOB):])
    pattern = True
  elif matcher.startswith(MATCHER_PREFIX_REGEX):
    p = matcher[len(MATCHER_PREFIX_REGEX):]
    pattern = True
  else:
    p = '^{}$'.format(re.escape(matcher))
  return re.compile(p), pattern



################################ Path Functions ################################

def desktop_mimeapps_filenames():
  '''
  Iterative over names of current desktop as defined in the XDG_CURRENT_DESKTOP
  environment variable.
  '''
  desktop = os.getenv(XDG_CURRENT_DESKTOP)
  if desktop:
    for d in desktop.split(':'):
      yield '{}-{}'.format(d.lower(), MIMEAPPS_LIST_FILE)



def mimeapps_directories(user=True, system=True, include_user_app_dir=True):
  '''
  Iterate over association file directories. The items are returned as tuples
  or lists so that each set can be iterated over in order of precedence as
  specified here:

      http://standards.freedesktop.org/mime-apps-spec/mime-apps-spec-latest.html#file

  '''
  my_name = 'mimeapps_directories'
  config_home = xdg.BaseDirectory.xdg_config_home
  if user:
    yield tuple(logging_debug_and_yield(
      my_name,
      (config_home,)
    ))

  if system:
    yield tuple(logging_debug_and_yield(
      my_name,
      (d for d in xdg.BaseDirectory.xdg_config_dirs if d != config_home)
    ))

  data_home = xdg.BaseDirectory.xdg_data_home

  if user and include_user_app_dir:
    yield tuple (logging_debug_and_yield(
      my_name,
      (os.path.join(data_home, APP_DIR),)
    ))

  if system:
    dpaths = list(
      os.path.join(d, APP_DIR)
      for d in xdg.BaseDirectory.xdg_data_dirs
      if d != data_home
    )
    yield tuple(logging_debug_and_yield(
      my_name,
      (
        os.path.join(d, APP_DIR)
        for d in xdg.BaseDirectory.xdg_data_dirs
        if d != data_home
      )
    ))



def desktop_directories(user=True, system=True):
  '''
  Iterate over desktop entry directories:

      https://specifications.freedesktop.org/menu-spec/menu-spec-latest.html#adding-items

  '''
  my_name = 'desktop_directories'
  data_home = xdg.BaseDirectory.xdg_data_home

  if user:
    yield from logging_debug_and_yield(
      my_name,
      (os.path.join(data_home, APP_DIR),)
    )

  if system:
    yield from logging_debug_and_yield(
      my_name,
      (
        os.path.join(d, APP_DIR)
        for d in xdg.BaseDirectory.xdg_data_dirs
        if d != data_home
      )
    )



def mimeapps_list_paths(
  current_desktop=False,
  *args,
  include_user_app_dir=False,
  **kwargs
):
  '''
  Find all association files and iterate over them in their order of precedence.
  Each returned element is a tuple containing the path to the file and a boolean
  to indicate if the file is desktop-specific.
  '''
  # Desktop-specific mimeapps.list files to check.
  dmals = list(desktop_mimeapps_filenames())

  for ds in mimeapps_directories(
    *args,
    include_user_app_dir=include_user_app_dir,
    **kwargs
  ):
    if current_desktop:
      if dmals:
        for dmal in dmals:
          for d in ds:
            yield os.path.join(d, dmal)
    for d in ds:
      yield os.path.join(d, MIMEAPPS_LIST_FILE)



def user_mimeapps_path(current_desktop=False):
  '''
  Get the user's association file.
  '''
  name = MIMEAPPS_LIST_FILE
  if current_desktop:
    try:
      name = next(desktop_mimeapps_filenames())
    except StopIteration:
      pass
  return os.path.join(xdg.BaseDirectory.xdg_config_home, name)



def desktop_paths(user=True, system=True, sort_per_dir=False):
  '''
  Iterate over all desktop files.
  '''
  for dpath in desktop_directories(
    user=user,
    system=system
  ):
    pattern = os.path.join(dpath, '*' + DESKTOP_EXTENSION)
    if sort_per_dir:
      ds = sorted(glob.glob(pattern))
    else:
      ds = glob.iglob(pattern)
    yield from ds



############################ mimeapps.list parsing #############################

def parse_associations(lines):
  '''
  Parse lines of an association file.
  '''
  section = None
  associations = collections.OrderedDict()
  for line in lines:
    line = line.strip()
    if not line or line[0] == '#':
      continue
    elif line[0] == '[' and line[-1] == ']':
      section = line[1:-1]
    else:
      try:
        mimetype, desktops = line.split('=',1)
      except ValueError:
        logging.warning('failed to parse line [{}]'.format(line))
      else:
        mimetype = mimetype.rstrip()
        # The standard only supports desktop file names. Strip diretory
        # components from the path to ensure. This ensures that joined paths
        # point to the "right" directory.
        desktops = list(os.path.basename(d.strip()) for d in desktops.split(';') if d)
        if desktops:
          try:
            associations[section][mimetype] = desktops
          except KeyError:
            associations[section] = {mimetype : desktops}
  return associations



def remove_empty_associations(assocs):
  '''
  Remove empty entries and sections.
  '''
  empty_sections = set()
  for section, entries in assocs.items():
    empty_keys = set()
    for key, values in entries.items():
      if not values:
        empty_keys.add(key)
    for k in empty_keys:
      del entries[k]
    if not entries:
      empty_sections.add(section)
  for s in empty_sections:
    del assocs[s]
  return assocs



def load_associations(path):
  '''
  Load association file.
  '''
  try:
    with open(path, 'r') as f:
      logging.debug('loading {}'.format(path))
      return parse_associations(f)
  except FileNotFoundError:
    return collections.OrderedDict()



def save_associations(path, assocs):
  '''
  Save associations to a file.
  '''
  assocs = remove_empty_associations(assocs)
  if assocs:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = len(assocs)
    logging.debug('saving {}'.format(path))
    with open(path, 'w') as f:
      for section, entries in assocs.items():
        if entries:
          f.write('[{}]\n'.format(section))
          for key, values in sorted(entries.items()):
            if values:
              f.write('{}={};\n'.format(key, ';'.join(values)))
      # Add newlines after each section if it is not the last.
      if n > 1:
        f.write('\n')
        n -= 1
  else:
    try:
      os.remove(path)
    except FileNotFoundError:
      pass



def iterate_associations(assocs, section, key):
  '''
  Iterate over associations in a file.

  assocs:
    Same type as that returend by parse_associations().
  '''
  try:
    yield from assocs[section][key]
  except (TypeError, KeyError):
    pass



def add_association(assocs, section, key, value):
  '''
  Add an association.
  '''
  if section and key and value:
    try:
      # Move it to the front of the list.
      assocs[section][key] = [value] + [x for x in assocs[section][key] if x != value]
    except KeyError:
      try:
        assocs[section][key] = [value]
      except KeyError:
        assocs[section] = {key : [value]}
  return assocs



def remove_association(assocs, section, key, value=None):
  '''
  Remove an association.
  '''
  if assocs and section and key:
    try:
      if not value:
        del assocs[section][key]
      else:
        assocs[section][key].remove(value)
        if not assocs[section][key]:
          del assocs[section][key]
    except (KeyError, ValueError):
      pass
  return assocs



################################ mimeinfo.cache ################################

def mimeinfo_caches(*args, **kwargs):
  '''
  Iterate over mimeinfo cache files.
  '''
  for dpath in desktop_directories(*args, **kwargs):
    yield os.path.join(dpath, MIMEINFO_CACHE_FILE)



# TODO
# Timestamp comparisons, etc.
def maybe_create_mimeinfo_cache(fpath, force=False):
  '''
  Get mimeinfo.cache data.

  Returns True if the file exists when finished. This may raise
  subprocess.CalledProcessError if the update command fails.
  '''
  dpath = os.path.dirname(fpath)
  pattern = os.path.join(dpath, '*' + DESKTOP_EXTENSION)
  if force or not os.path.exists(fpath):
    if glob.glob(pattern):
      cmd = [EXE_UPDATE_DESKTOP_DATABASE, dpath]
      logging.debug(quote_cmd(cmd))
      subprocess.run(cmd)
      return True
    else:
      return False
  else:
    return True



def update_mimeinfo_caches(*args, **kwargs):
  '''
  Attempt to update mimeinfo caches.
  '''
  for fpath in mimeinfo_caches(*args, **kwargs):
    maybe_create_mimeinfo_cache(fpath, force=True)



################################ Desktop files #################################

def desktop_entry(path, none_if_error=False):
  '''
  Load a desktop entry. Some minor corrections are applied to the desktop entry
  here so use this function whenever a desktop entry is needed.
  '''
  de = xdg.DesktopEntry.DesktopEntry()
  # This is necessary because the filename attribute is only set in the "new"
  # method for some reason.
  de.filename = path

  logging.debug('parsing {}'.format(path))
  if none_if_error:
    try:
      # This will raise ParsingError if the file is not found.
      de.parse(path)
    except xdg.DesktopEntry.ParsingError as e:
      logging.debug('error loading {}: {}'.format(path, e))
      return None
  else:
    de.parse(path)
  return de



def create_desktop_entry(path, name, cmd, mimetypes, is_term=False):
  '''
  Create a minimal desktop file.
  '''
  if name and cmd and mimetypes:
    term = 'true' if is_term else 'false'
    logging.debug('creating {}'.format(path))
    with open(path, 'w') as f:
      f.write('''[Desktop Entry]
Type=Application
Name={}
Exec={}
MimeType={};
Terminal={}
NoDisplay=true
Comment=Created by {}
'''.format(
        name,
        ' '.join(exec_quote_exec(cmd)),
        ';'.join(mimetypes),
        term,
        NAME
      ))
  else:
    raise ValueError('create_desktop_entry: empty values')



def exec_quote_exec(cmd):
  '''
  Quote a command for an Exec entry in a desktop file.
  '''
  for word in cmd:
    for c in EXEC_RESERVED:
      if c in word:
        yield exec_quote_word(''.join(word))
        break
    else:
      yield word



def exec_quote_word(word):
  '''
  Quote a word and escape characters as necessary.
  '''
  yield '"'
  for c in word:
    if c in EXEC_ESCAPED:
      yield '\\c'
    else:
      yield c
  yield '"'



def parse_unexpandable_field_codes(word, field_codes):
  '''
  Interpolate field codes within an unexpandable command word.
  '''
  field_code = False
  for c in word:
    if field_code:
      field_code = False
      try:
        yield field_codes[c]
      except KeyError:
        pass
    elif c == '%':
      field_code = True
    else:
      yield c



def parse_field_codes(word, name, args=None, icon=None, path=None, omit_empty=False):
  '''
  Interpolate field codes within a single command word.
  '''
  if not args:
    args = tuple()
  if word == '%i':
    if icon:
      yield '--icon'
      yield icon
  elif word == '%F':
    for a in args:
      p = ensure_path(a)
      if p:
        yield p
  elif word == '%U':
    for a in args:
      yield ensure_url(a)
  elif word in ('%f', '%u') and not args:
    pass
  else:
    field_codes = {
      '%' : '%',
      'c' : name,
      'k' : path if path else '',
      'f' : '',
      'u' : ''
    }
    # Use first (and only) value if there is one.
    for arg in args:
      p = ensure_path(arg)
      field_codes['f'] = p if p else ''
      field_codes['u'] = ensure_url(arg)
      break
    interpolated_word = ''.join(parse_unexpandable_field_codes(word, field_codes))
    if interpolated_word \
    or not omit_empty \
    or not word in ('%{}'.format(c) for c in 'kfu'):
      yield ''.join(parse_unexpandable_field_codes(word, field_codes))



def desktop_entry_to_cmds(de, args=None, term_cmd=None):
  '''
  Interpolate the Exec entry of the desktop file and iterate over the resulting
  commands.
  '''
  exe = de.getExec()
  icon = de.getIcon()
  name = de.getName()
  is_term = de.getTerminal()
  path = de.filename

  return exec_field_to_cmds(
    exe, args, name, icon=icon, path=path, is_term=is_term, term_cmd=term_cmd
  )



def exec_field_to_cmds(exe, args, name, icon=None, path=None, is_term=False, term_cmd=None):
  '''
  Interpolate a Desktop Entry Exec field and maybe insert it into a terminal command.
  '''

  for app_cmd in exec_field_to_cmds_without_term(exe, args, name, icon=icon, path=path):
    if is_term and term_cmd:
      yield list(interpolate_term_cmd(term_cmd, app_cmd))
    else:
      yield app_cmd



def exec_field_to_cmds_without_term(exe, args, name, icon=None, path=None):
  '''
  Interpolate a Desktop Entry Exec field.
  '''
  test_exe = exe.replace('%%', '')
  single = False
  codes = set()
  for c in 'fuFU':
    if ('%' + c) in test_exe:
      codes.add(c)
      if c in 'fu':
        single = True
  if len(codes) > 1:
    raise xdg.DesktopEntry.ValidationError(
      'command should only contain at most one of the following: {}'.format(
        ' '.join(('%'+x) for x in codes)
      )
    )

  words = shlex.split(exe)
  if not args:
    argss = tuple((tuple(),))
  elif single:
    argss = ((a,) for a in args)
  else:
    argss = (args,)
  for aa in argss:
    yield list(itertools.chain.from_iterable(
      parse_field_codes(w, name, icon=icon, path=path, args=aa)
      for w in words
    ))



################################ MimeappsCache #################################

class MimeappsCache(object):
  def __init__(self):
    self.associations = dict()



  def clear(self):
    self.associations.clear()



  def __getitem__(self, path):
    try:
      return self.associations[path]
    except KeyError:
      assocs = load_associations(path)
      self.associations[path] = assocs
      return assocs


  def __setitem__(self, path, assocs):
    self.associations.__setitem__(path, assocs)



  def __delitem__(self, path):
    self.associations.__delitem__(path)



#################################### Mimeo #####################################

class Mimeo(object):
  def __init__(
    self,
    user=True,
    system=True,
    include_deprecated=False,
    term_cmd=None,
    by_content_first=False,
    by_content_only=False,
    by_name_only=False,
    follow=True,
    current_desktop=False,
    mimeo_assocs=None,
    none_on_de_parsing_err=True,
  ):
    self.user = user
    self.system = system
    self.include_deprecated = include_deprecated
    self.term_cmd = term_cmd
    self.by_content_first=by_content_first
    self.by_content_only=by_content_only
    self.by_name_only=by_name_only
    self.follow=follow
    self.current_desktop=current_desktop
    self.mimeo_assocs=mimeo_assocs
    self.none_on_de_parsing_err = none_on_de_parsing_err

    self.associations = dict()
    self.seen_mimetypes = set()
    self.reset()



  def reset(self):
    '''
    Clear cached data.
    '''
    self.mimetypes_knownfiles = [os.path.expanduser('~/.mime.types')] + mimetypes.knownfiles
    self.associations.clear()
    self.seen_mimetypes.clear()
    self.initialize()



  def initialize(self):
    '''
    Initialize mimetypes internal data structures etc.
    '''
    mimetypes.init(self.mimetypes_knownfiles)



  def load_mimeo_associations(self, fpath=None):
    '''
    Load custom Mimeo association. If fpath is None,
    '''
    self.mimeo_assocs = None
    if fpath is None:
      for fpath in default_mimeo_associations_paths():
        try:
          self.mimeo_assocs = list(parse_mimeo_associations(fpath))
          break
        except FileNotFoundError:
          continue
    elif fpath:
      self.mimeo_assocs = list(parse_mimeo_associations(fpath))



  def get_associations(self, path):
    '''
    Get possibly cached associations from the given path.
    '''
    try:
      return self.associations[path]
    except KeyError:
      try:
        assocs = load_associations(path)
      except FileNotFoundError:
        return None
      else:
        self.associations[path] = assocs
        return assocs



  def mimeapps_directories(self):
    '''
    Iterate over application directory paths.
    '''
    yield from itertools.chain.from_iterable(mimeapps_directories(
      user=self.user,
      system=self.system
    ))



  def mimeapps_list_paths(self):
    '''
    Iterate over mimeapps.list paths.
    '''
    return mimeapps_list_paths(
      current_desktop=True,
      include_user_app_dir=self.include_deprecated
    )



  def desktop_paths(self, sort_per_dir=False):
    '''
    Iterate over desktop entry paths.
    '''
    return desktop_paths(
      user=self.user,
      system=self.system,
      sort_per_dir=sort_per_dir
    )



  def mimeinfo_caches(self):
    '''
    Update mimeinfo caches.
    '''
    return mimeinfo_caches(
      user=self.user,
      system=self.system
    )



  def search_desktop_paths(self, ds, first_only=False):
    '''
    Search for desktop files. The order is arbitrary.
    '''
    ds = set(ensure_desktop_names(ds))
    found = set()
    for dpath in desktop_directories(user=self.user, system=self.system):
      for d in ds:
        path = os.path.join(dpath, d)
        if os.path.exists(path):
          yield path
          if first_only:
            found.add(d)
      if first_only:
        ds -= found



  def update_mimeinfo_caches(self):
    '''
    Update mimeinfo caches.
    '''
    update_mimeinfo_caches(
      user=self.user,
      system=self.system
    )



  def mimeapps_list_paths_and_assocs(self):
    '''
    Iterate over mimeapps.list files and their paths.
    '''
    for path in self.mimeapps_list_paths():
      yield path, self.get_associations(path)



  def section_entries(self, paths, sections):
    '''
    Iterate over all entries for the given paths and sections.
    '''
    for path in paths:
      assocs = self.get_associations(path)
      if not assocs:
        continue
      for section in sections:
        try:
          for e in assocs[section]:
            yield e
        except KeyError:
          pass



  def associated_desktop_paths(self, mimetype):
    '''
    Iterate over desktop files associated with a given MIME-type.
    '''
    # The added associations are given in order of preference so a list must be
    # used.
    added = list()
    blacklist = set()
    for path, assocs in self.mimeapps_list_paths_and_assocs():
      blacklist.update(iterate_associations(assocs, REMOVED_ASSOCIATIONS_SECTION, mimetype))

      added = list(a for a in added if a not in blacklist)
      added.extend(
        a for a in iterate_associations(assocs, ADDED_ASSOCIATIONS_SECTION, mimetype)
        if a not in blacklist and a not in added
      )

      dpath = os.path.dirname(path)
      mimeinfo_cache_path = os.path.join(dpath, MIMEINFO_CACHE_FILE)

      mimeinfo_cache_assocs = self.get_associations(mimeinfo_cache_path)
      local_associations = added.copy()
      local_associations.extend(
        x for x in iterate_associations(mimeinfo_cache_assocs, MIME_CACHE_SECTION, mimetype)
        if x not in blacklist and x not in added
      )

      for d in local_associations:
        desktop_path = os.path.join(dpath, d)
        if os.path.exists(desktop_path):
          yield desktop_path



  def default_desktop_filenames(self, mimetype):
    '''
    Iterate over default desktop filenames.
    '''
    for path in self.mimeapps_list_paths():
      assocs = self.get_associations(path)
      yield from iterate_associations(assocs, DEFAULT_APPLICATIONS_SECTION, mimetype)



  @unique_items
  def mimetype_to_desktop_filepaths(
    self,
    mimetype,
    at_least_one=False,
    first_only=False,
    only_existing=False
  ):
    '''
    Iterate over default desktop paths then over associated desktop paths.
    '''
    stripped_mimetype = strip_mimetype(mimetype)
    if stripped_mimetype != mimetype:
      mimetypes = (mimetype, stripped_mimetype)
    else:
      mimetypes = (mimetype,)
    found_one = False
    for mimetype in mimetypes:
      defaults = list(self.default_desktop_filenames(mimetype))
      if defaults:
        for d in defaults:
          for dpath in self.mimeapps_directories():
            fpath = os.path.join(dpath, d)
            if not only_existing or os.path.exists(fpath):
              yield fpath
              if first_only:
                return
              found_one = True
      for fpath in self.associated_desktop_paths(mimetype):
        if not only_existing or os.path.exists(fpath):
          yield fpath
          if first_only:
            return
          found_one = True
    if not found_one:
      logging.debug('failed to determine at least one desktop for {}'.format(mimetype))
      if at_least_one:
        yield None


  def arg_to_mimetypes(self, arg, at_least_one=False, first_only=False):
    '''
    Match argument to MIME-types. This will not return anything if the match
    fails.
    '''
    found_one = False

    path = ensure_path(arg)
    if path:
      for m in mimetypes_from_path(
        path,
        follow_symlinks=self.follow,
        content_first=self.by_content_first,
        content_only=self.by_content_only,
        name_only=self.by_name_only
      ):
        yield m
        if first_only:
          return
        found_one = True

    parsed_url = urllib.parse.urlparse(arg)
    scheme = parsed_url.scheme
    if scheme:
      yield MIMETYPE_SCHEME_FMT.format(scheme)
      if first_only:
        return
      found_one = True

    for m in self.matching_mimetypes(arg, ensure_known=True):
      yield m
      if first_only:
        return
      found_one = True

    if not found_one:
      logging.warning('failed to determine at least one MIME-type for {}'.format(arg))
      if at_least_one:
        yield None



  def args_to_mimetypes(self, args, at_least_one=False, first_only=False):
    '''
    Match arguments to MIME-types.
    '''
    for a in args:
      for m in self.arg_to_mimetypes(a, at_least_one=at_least_one, first_only=first_only):
        yield a, m



  def mimetypes_to_desktop_paths(
    self,
    ms,
    at_least_one=False,
    first_only=False,
    only_existing=False
  ):
    '''
    Match MIME-types to desktop paths.
    '''
    for m in ms:
      for d in self.mimetype_to_desktop_filepaths(
        m,
        at_least_one=at_least_one,
        first_only=first_only,
        only_existing=only_existing
      ):
        yield m, d



  @unique_items
  def args_to_desktop_paths(
    self,
    args,
    at_least_one=False,
    first_only=False,
    only_existing=False
  ):
    '''
    Match arguments to desktop paths.
    '''
    for a, m in self.args_to_mimetypes(
      args,
      at_least_one=at_least_one,
      first_only=first_only
    ):
      for d in self.mimetype_to_desktop_filepaths(
        m,
        at_least_one=at_least_one,
        first_only=first_only,
        only_existing=only_existing
      ):
        yield a, d
        if first_only:
          break



  def args_to_desktop_entries(
    self,
    args=None,
    at_least_one=False,
    first_only=False,
  ):
    '''
    Match arguments to desktop entries.
    '''
    for a, d in self.args_to_desktop_paths(
      args,
      at_least_one=at_least_one,
      first_only=first_only,
      only_existing=True
    ):
      if d is None:
        yield a, None
      else:
        yield a, desktop_entry(d)



  def args_to_cmd_precursors(
    self,
    args,
    at_least_one=False,
    first_only=False,
  ):
    '''
    Return tuples of custom commands and desktop paths for the arguments. These
    can be passed to the collection function and then converted to a command.
    '''
    if not isinstance(args, list):
      args = list(args)
    yielded = set()
    if self.mimeo_assocs:
      for a, cmd in args_to_custom_cmds(
        self.mimeo_assocs,
        args,
        first_only=first_only
      ):
        yield a, (cmd, None)
        yielded.add(a)

    if first_only:
      remaining = (a for a in args if a not in yielded)
    else:
      remaining = args

    for a, d in self.args_to_desktop_paths(
      remaining,
      first_only=first_only,
      only_existing=True
    ):
      yield a, (None, d)
      yielded.add(a)

    if at_least_one:
      for a in args:
        if a not in yielded:
          logging.warning('failed to determine command precursor for {}'.format(a))
          if at_least_one:
            yield a, None



  def args_to_cmds(
    self,
    args,
    first_only=False,
  ):
    '''
    Return commands for the given arguments.
    '''
    a_to_b = self.args_to_cmd_precursors(
      args,
      first_only=first_only,
      at_least_one=True
    )
    a_by_b = modify_and_collect(a_to_b, swap=True)
    for pc, aa in a_by_b.items():
      if pc is None:
        logging.warning('failed to determine command(s) for {}'.format(quote_cmd(aa)))
      elif pc[0] is not None:
        for c in exec_field_to_cmds(pc[0], aa, 'User Command'):
          yield c
      else:
        de = desktop_entry(pc[1])
        yield from desktop_entry_to_cmds(de, args=aa, term_cmd=self.term_cmd)



  def known_mimetypes(self):
    '''
    Return a set of known MIME-types.
    '''
    if not self.seen_mimetypes:
      paths = mimeapps_list_paths(
        current_desktop=True,
        user=True,
        system=True,
        include_user_app_dir=self.include_deprecated
      )
      sections = (ADDED_ASSOCIATIONS_SECTION, DEFAULT_APPLICATIONS_SECTION)
      self.seen_mimetypes.update(self.section_entries(paths, sections))

      paths = mimeinfo_caches(user=True, system=True)
      sections = (MIME_CACHE_SECTION,)
      self.seen_mimetypes.update(self.section_entries(paths, sections))

      for path in self.mimetypes_knownfiles:
        try:
          with open(path, 'r') as f:
            logging.debug('loading MIME-types from {}'.format(path))
            for line in f:
              m = MIMETYPES_KNOWNFILES_REGEX.search(line)
              if m:
                self.seen_mimetypes.add(m.group(1))
        except FileNotFoundError:
          pass

    return self.seen_mimetypes



  def matching_mimetypes(self, matcher, ensure_known=False):
    '''
    Iterate over all known MIME-types matched by the given matcher.
    '''
    regex, is_pattern = mimetype_regex(matcher)
    if is_pattern or ensure_known:
      for m in self.known_mimetypes():
        if regex.match(m):
          yield m
    else:
      yield matcher



  def modify_associations(self, op, matcher, desktops=None):
    '''
    Modify associations.
    '''
    path = user_mimeapps_path(current_desktop=self.current_desktop)
    assocs = self.get_associations(path)

    if matcher:
      mimetypes = self.matching_mimetypes(matcher)
    elif desktops:
      desktops = set(desktops)
      mimetypes = set()
      for d in self.search_desktop_paths(desktops, first_only=True):
        de = desktop_entry(d)
        mimetypes.update(de.getMimeTypes())

    if op in ASSOCIATION_ADDERS:
      if desktops:
        for s, m, d in itertools.product(
          ASSOCIATION_ADDERS[op],
          mimetypes,
          desktops
        ):
          assocs = add_association(assocs, s, m, d)

    elif op in ASSOCIATION_REMOVERS:
      if desktops:
        for s, m, d in itertools.product(
          ASSOCIATION_REMOVERS[op],
          mimetypes,
          desktops
        ):
          assocs = remove_association(assocs, s, m, d)
      else:
        for s, m in itertools.product(
          ASSOCIATION_REMOVERS[op],
          mimetypes,
        ):
          assocs = remove_association(assocs, s, m)

    save_associations(path, assocs)



  def desktop_paths_to_desktop_entries(
    self, ds=None, first_only=False
  ):
    '''
    Iterate over desktops and their desktop entries.
    '''
    if ds:
      ds = self.search_desktop_paths(ds, first_only=first_only)
    else:
      ds = self.desktop_paths()
    for d in ds:
      de = desktop_entry(d, none_if_error=self.none_on_de_parsing_err)
      yield d, de



  def desktop_paths_to_desktop_fields(
    self, field, typ='string', ds=None, first_only=False
  ):
    '''
    Iterate over desktops and the given field of their desktop entries.
    '''
    for d, de in self.desktop_paths_to_desktop_entries(
      ds=ds, first_only=first_only
    ):
      if de:
        for x in de.get(field, type=typ, list=True):
          yield d, x
      else:
        yield d, None



  def executables_to_desktop_paths(self, exes=None):
    '''
    Match executables to desktop entries.
    '''
    if exes:
      exes = list((e, which(e)) for e in exes)
    for d, exec_field in self.desktop_paths_to_desktop_fields('Exec'):
      if exec_field:
        ef1 = shlex.split(exec_field)[0]
        c = which(ef1)
        if not c:
          c = which(os.path.basename(ef1))
        if not c:
          continue
        elif exes:
          for e, p in exes:
            if p == c:
              yield e, d
              break
        else:
          yield c, d



  def desktop_paths_to_cmds(self, ds=None, args=None, first_only=False):
    '''
    Iterate over desktops and their associated MIME-types.
    '''
    for d, de in self.desktop_paths_to_desktop_entries(ds=ds, first_only=first_only):
      for c in desktop_entry_to_cmds(de, args=args, term_cmd=self.term_cmd):
        yield d, c



############################### Argument parsing ###############################

class DisplayAssociationHelp(argparse.Action):
  def __call__(self, parser, namespace, values, option_string=None):
    print(association_help)
    sys.exit(0)



class DisplayMimemanHelp(argparse.Action):
  def __call__(self, parser, namespace, values, option_string=None):
    print(mimeman_help)
    sys.exit(0)



association_help = '''USAGE
  The associations file contains commands followed by regular expressions, all
  on separate lines. It enables the user to associate arbitrary strings with
  applications. This relies only on the argument string itself and is
  independent of any associated file or MIME-type.

  Association files can be specified on the command line. {name} will also
  check for association files in default locations. Check the main help message
  for details.

COMMANDS
  The command is parsed as a .desktop "Exec" field and may thus contain
  variables such as "%f" and "%F". In the absence of such, the input argument is
  passed as the final argument to the command.

  See the .desktop documentation for rules about quoting etc.:

    http://standards.freedesktop.org/desktop-entry-spec/desktop-entry-spec-latest.html#exec-variables

REGULAR EXPRESSIONS
  The regular expressions are indented by exactly two spaces ("  ") and are
  associated with the previous command. Any argument that matches a regular
  expression will be opened with its associated command.

  The two-space indentation allows the use of regular expressions that begin
  with whitespace.

OTHER
  The file may contain empty lines and comments. The first character of a
  comment line must be "#".

EXAMPLES
  Associate HTTP and HTTPS URIs with Firefox:

    /usr/bin/firefox %U
      ^https?://


  Associate Perl (*.pl),  Python (*.py) and text (*.txt) files with gVim:

    /usr/bin/gvim %F
      \.p[ly]$
      \.txt$


  Enqueue various media files in a running instance of VLC:

    /usr/bin/vlc --one-instance --playlist-enqueue %F
      \.mp3$
      \.flac$
      \.avi$
      \.mpg$
      \.flv$
'''.format(name=NAME)



mimeman_help = '''MIME-manager Help

  <MIME-type matcher>
    MIME-types may be specified in one of three ways using different
    "MIME-type matchers":

      Direct Match
        The matcher is interpretted as a MIME-type string, e.g. "text/x-python".

      Shell-style Globbing
        If the matcher begins with "{glob_prefix}" then the rest of it will be
        interpretted as a shell-style globbing pattern and it will be matched
        against all known MIME-types.

        For example, "{glob_prefix}text/x-*" would be expanded to all known
        MIME-types beginning with "text/x-".

      Regular Expressions
        If the matcher begins "{regex_prefix}" then the rest of it will be
        interpretted as a (Perl-style) regular expression and it will be matched
        against all known MIME-types.

        For example, "{regex_prefix}^text/x-.*" would be equivalent to
        "{glob_prefix}text/x-*"

  <desktop.file>
    The name of a desktop file, with the ".desktop" extension. Desktop files
    are found in the "applications" sub-directory of directories in
    $XDG_DATA_HOME and $XDG_DATA_DIRS

  <Name>
    The value of the "Name" key in a desktop file.

  <Exec>
    The value of the "Exec" key in a desktop file.

    See
    http://standards.freedesktop.org/desktop-entry-spec/desktop-entry-spec-latest.html#exec-variables
    for details.

  EXAMPLES

    Set firefox as the prefered browser for all associated MIME-types:

      {prog} --prefer firefox.desktop

    To set multiple preferences, pass the option multiple times:

      {prog} --prefer firefox.desktop --prefer vlc.desktop

    If both desktop arguments are passed together then their MIME-types will be
    pooled and both will be set as preferred applications for the pool, which is
    unlikely to be what you want:

      # Don't do this. Use the previous command instead.
      {prog} --prefer firefox.desktop vlc.desktop

    This is a consequence of the way the command is parsed. It is usually used
    with a MIME-type matcher.  For example, to set vlc and mplayer as preferred
    applications for videos, with vlc taking precedence, you could use:

      {prog} --prefer 'glob:video/*' vlc.desktop mplayer.desktop

    To check which MIME-types will be matched, use:

      {prog} --mimetype 'glob:video/*'

    It is possible to create custom desktop files for your own commnds. For
    example, to create one for Feh and associate all images with it:

      {prog} --create feh.desktop Feh 'feh %F -F -Z' '{glob_prefix}image/*'

    Now set feh.desktop as the default for PNG and JPEG images:

      {prog} --prefer '{regex_prefix}^image/(png|jpe?g)$' feh.desktop

    Instead of passing <MIME-type matcher>, the MIME-type can be parsed from a
    file path. For example, to prefer feh.desktop for all PNG images, use:

      {prog} --prefer /path/to/foo.png feh.desktop

'''.format(
  prog=NAME,
  glob_prefix=MATCHER_PREFIX_GLOB,
  regex_prefix=MATCHER_PREFIX_REGEX
)




class DisplayFilepathHelp(argparse.Action):
  def __call__(self, parser, namespace, values, option_string=None):
    # Keep this in the function to ensure that it is always updated right before
    # it is printed rather than set when the module is loaded.
    print('''CUSTOM ASSOCIATION FILES

  If --assoc is not passed then the following paths will be checked for
  custom associations, in order:

    {assocs_paths}

  The first that exists, if any, will be used. Pass en empty argument to disable
  the check. See --assoc-help for details.



DEFAULT ARGUMENTS

  Additional command-line arguments will be read from

    {dapath}

  The file should contain shell-readable arguments on the first line and nothing
  else, e.g.

    --term 'urxvt -e %s' --deprecated"

  The line will be parsed with shlex.split and prepended to the given arguments.



DEPRECATED FILES

  The following path is deprecated:

    {old_appdir}

  When the --deprecated flag is used, {applist_name} in this directory will be
  checked and used. For compatibility with applications that use the older
  standards, copy

    {applist}

  to

    {old_applist}

  and

    {old_deflist}

  or just merge the relevant sections. Even when --deprecated is passed, Mimeo
  will not apply changes to {applist_name} or {deflist_name} in that directory.

'''.format(
      applist_name=MIMEAPPS_LIST_FILE,
      deflist_name=DEFAULTS_LIST_FILE,
      assocs_paths='\n    '.join(default_mimeo_associations_paths()),
      dapath=default_arguments_path(),
      applist=os.path.join(xdg.BaseDirectory.xdg_config_home, MIMEAPPS_LIST_FILE),
      old_applist=os.path.join(xdg.BaseDirectory.xdg_data_home, APP_DIR, MIMEAPPS_LIST_FILE),
      old_deflist=os.path.join(xdg.BaseDirectory.xdg_data_home, APP_DIR, DEFAULTS_LIST_FILE),
      old_appdir=os.path.join(xdg.BaseDirectory.xdg_data_home, APP_DIR)
    ))
    sys.exit(0)



def get_argparser():
  parser = argparse.ArgumentParser(
    prog=NAME,
    description='Open files using MIME-type and custom user associations.',
    usage="%(prog)s [options] [<arg> ...]",
    epilog='If no operation is specified, the commands determined by "--command" will be run, i.e. the passed arguments will be opened. See --filepath-help for further configuration options such as passing default arguments.'
  )

  query_op_group = parser.add_argument_group(
    'Query Operations',
    'Operations to obtain information.'
  )

  query_op_group.add_argument(
    '--assoc-help', action=DisplayAssociationHelp, nargs=0,
    help='Display information about the custom associations file.'
  )

  query_op_group.add_argument(
    '--mimeman-help', action=DisplayMimemanHelp, nargs=0,
    help='Display information about managing MIME-type associations.'
  )

  query_op_group.add_argument(
    '--filepath-help', action=DisplayFilepathHelp, nargs=0,
    help='Display information about configuration and data filepaths.'
  )

  query_op_group.add_argument(
    '-c', '--command', action='store_true',
    help='Print the full command(s) and exit.'
  )

  query_op_group.add_argument(
    '-d', '--desktop', action='store_true',
    help='Print the associated desktop file names and paths and exit.'
  )

  query_op_group.add_argument(
    '-m', '--mimetype', action='store_true',
    help='Print the detected MIME-type(s) for the given arguments and exit. The arguments may be paths, %(prog)s MIME-type matchers, or URIs. If no arguments are given, print all known MIME-types.'
  )

  query_op_group.add_argument(
    '--finddesk', action='store_true',
    help='Return the paths to the given desktops if they exist.'
  )

  query_op_group.add_argument(
    '--mime2desk', action='store_true',
    help='List desktop files associated with the given MIME-types.'
  )

  query_op_group.add_argument(
    '--app2desk', action='store_true',
    help='List desktop files that use the given executables and exit. If no arguments are given then the executables of every desktop file will be listed.'
  )

  query_op_group.add_argument(
    '--desk2field',  metavar='<desktop entry field>',
    help='List the values of a desktop entry field per desktop, e.g. "Exec" or "MimeType".'
  )

  query_op_group.add_argument(
    '--mimeapps-list', action='store_true',
    help='Print the paths to detected mimeapps.list files.'
  )

  mod_op_group = parser.add_argument_group(
    'Modification Operations',
    'Operations to change associations and preferences. If no MIME-type matcher is given then the MIME-types in the desktop files will be used.'
  )

  mod_op_group.add_argument(
    '--update', action='store_true',
    help='Update associations and cache files.'
  )

  mod_op_group.add_argument(
    '--add', action='append', nargs='+',
    metavar=ASSOCIATION_MODIFICATION_METAVAR,
    help='Associate MIME-types with desktop files. See "--mimeman-help" for more information.'
  )

  mod_op_group.add_argument(
    '--unadd', action='append', nargs='+',
    metavar=ASSOCIATION_MODIFICATION_METAVAR,
    help='Undo an --add operation.'
  )

  mod_op_group.add_argument(
    '--remove', action='append', nargs='+',
    metavar=ASSOCIATION_MODIFICATION_METAVAR,
    help='Remove associations by adding them to the "{}" section of mimeapps.list. This can effectively hide system-associations from the user. This does not affect default. Use "--clear" to forget a user-association. See "--mimeman-help" for more information.'.format(REMOVED_ASSOCIATIONS_SECTION)
  )

  mod_op_group.add_argument(
    '--unremove', action='append', nargs='+',
    metavar=ASSOCIATION_MODIFICATION_METAVAR,
    help='Undo a --remove operation.'
  )

  mod_op_group.add_argument(
    '--prefer', action='append', nargs='+',
    metavar=ASSOCIATION_MODIFICATION_METAVAR,
    help='Set a default association. See "--mimeman-help" for more information.'
  )

  mod_op_group.add_argument(
    '--unprefer', action='append', nargs='+',
    metavar=ASSOCIATION_MODIFICATION_METAVAR,
    help='Undo a --prefer operation.'
  )

  mod_op_group.add_argument(
    '--clear', action='append', nargs='+',
    metavar=ASSOCIATION_MODIFICATION_METAVAR,
    help='Clear associations. If no desktop files are specified, all associations for the MIME-type(s) will be cleared. To clear all associations for a desktop file, use "--clear \'glob:*\' {}". This affects defaults. See "--mimeman-help" for more information.'.format(ASSOCIATION_MODIFICATION_METAVAR[1])
  )

  mod_op_group.add_argument(
  '--create', action='append', nargs=5, default=[],
  metavar=('<filename>', '<Name>', '<Exec>', '<MIME-type matcher or "">', '<"term" or "">'),
  help='Create a minimal desktop file. Edit the created file if necessary. If an empty string is passed instead of a MIME-type matcher, the file will not specify any MIME-type associations. The fifth argument indicates if "Terminal" should be set to "true" in the created file. See "--mimeman-help" for more information. The created files are saved in $XDG_DATA_HOME/{appdir} (~/.local/share/{appdir} by default).'.format(appdir=APP_DIR)
  )



  conf_group = parser.add_argument_group(
    'Configuration',
    'Various configuration options.'
  )
  conf_group.add_argument(
    '-a', '--assoc', metavar='<filepath>',
    help='Specify a file that associates regular expressions with custom commands. This can be used for opening URLs, for example. See "--assoc-help" for details. See --filepath-help for default paths.'
  )

  conf_group.add_argument(
    '--no-assoc', dest='use_default_assoc', action='store_false',
    help='Do not use the default associations file.'
  )

  conf_group.add_argument(
    '--no-def-args', dest='use_default_args', action='store_false',
    help='Omit the default arguments.'
  )

  conf_group.add_argument(
    '--user', action='store_true',
    help='Restrict operations to user files.'
  )

  conf_group.add_argument(
    '--system', action='store_true',
    help='Restrict operations to system files. This is mostly useful for updating system desktop files and MIME information as root.'
  )

  conf_group.add_argument(
    '-q', '--quiet', action='store_true',
    help='Suppress all output from launched applications.'
  )

  conf_group.add_argument(
    '--term', metavar='<cmd>', action='store',
    help='Terminal command to use when launching applications with desktop files that specify "Terminal=true". It will be split into words using shlex.split. A word equal to "%%s" will be replaced by the separate words of the application command. A word equal to "\'%%s\'" will be replaced by a single word containing the joined words of the application command. Any other instance of "%%s" within a word will be replaced by the joined words of the application command. If "%%s" does not appear within the terminal command then the separate words of the application command will be appended to the end of the command. Examples: "urxvt -e", "urxvt -e %%s", "xterm -e bash -c \'%%s\'". A literal "%%" may be escaped with "%%%%". Use the default arguments file or a shell alias to automatically pass this argument.'
  )

  conf_group.add_argument(
    '--cmd-prefix', nargs='+', metavar=('<cmd>', '<arg>'),
    help='Prefix commands and arguments to the desktop commands. For example, this can be used to run commands with sudo, gksudo, etc. while still running $(prog) as a regular user.'
  )

  conf_group.add_argument(
    '--by-content-only', action='store_true',
    help='Determine MIME-type of files from the content only.'
  )

  conf_group.add_argument(
    '--by-content-first', action='store_true',
    help='Check file content before name when determining MIME-type.'
  )

  conf_group.add_argument(
    '--by-name-only', action='store_true',
    help='Determine MIME-type of files from the name only.'
  )

  conf_group.add_argument(
    '--no-follow', action='store_true',
    help='Do not follow symlinks.'
  )

  conf_group.add_argument(
    '--deprecated', action='store_true',
    help='Use deprecated directories. See --filepath-help for details.'
  )

  conf_group.add_argument(
    '--current-desktop', action='store_true',
    help='Modify associations of the current desktop as specified in ${xcd}. Ignored if ${xcd} is not set.'.format(xcd=XDG_CURRENT_DESKTOP)
  )

  conf_group.add_argument(
    '--debug', action='store_true',
    help='Enable debugging messages.'
  )

  conf_group.add_argument(
    '--full-path', action='store_true',
    help='Return full paths to desktop files for some outputs..'
  )

  conf_group.add_argument(
    '--show-all', action='store_true',
    help='For some output, show all possibilities rather than just the first. This can be used with --command for example.'
  )

  conf_group.add_argument(
    '--swap', action='store_true',
    help='Swap the way displayed information is organized, e.g. display input arguments per MIME-type instead of MIME-types per input argument with --mimetype. This does not work for all query operations.'
  )

  parser.add_argument('args', nargs='*', metavar='<arg>')

  return parser



##################################### Main #####################################

def main(args=None):
  if not args:
    args = sys.argv[1:]
  parser = get_argparser()
  pargs = parser.parse_args(args)

  if pargs.use_default_args:
    extra_args = default_arguments()
    if extra_args:
      logging.debug('prepending arguments: {}'.format(quote_cmd(extra_args)))
      args = extra_args + args
    pargs = parser.parse_args(args)

  mimeo = Mimeo(
    user=(not pargs.system),
    system=(not pargs.user),
    include_deprecated=pargs.deprecated,
    term_cmd=pargs.term,
    by_content_first=pargs.by_content_first,
    by_content_only=pargs.by_content_only,
    by_name_only=pargs.by_name_only,
    follow=(not pargs.no_follow),
    current_desktop=pargs.current_desktop,
  )
  if pargs.assoc or pargs.use_default_assoc:
    mimeo.load_mimeo_associations(fpath=pargs.assoc)

  if pargs.create:
    appdir = xdg.BaseDirectory.save_data_path(APP_DIR)
    for fname, name, exe, matcher, is_term in pargs.create:
      fname = os.path.basename(fname)
      if not fname.endswith(DESKTOP_EXTENSION):
        fname += DESKTOP_EXTENSION
      path = os.path.join(appdir, fname)
      exe = shlex.split(exe)
      mimetypes = sorted(mimeo.matching_mimetypes(matcher))
      is_term = bool(is_term)
      create_desktop_entry(path, name, exe, mimetypes, is_term=is_term)



  for op in (
    'add',
    'unadd',
    'remove',
    'unremove',
    'prefer',
    'unprefer',
    'clear',
  ):
    op_argss = getattr(pargs, op)
    if op_argss:
      for op_args in op_argss:
        # Make it possible to get the MIME-type from a file.
        if os.path.exists(op_args[0]):
          ms = mimeo.arg_to_mimetypes(op_args[0], at_least_one=True, first_only=True)
          matcher = next(ms)
          ds = op_args[1:]
        # No matcher if the first argument contains the desktop extension
        # or if the operation is an adder and there are no further arguments.
        elif op_args[0].endswith(DESKTOP_EXTENSION) \
        or (op in ASSOCIATION_ADDERS and not op_args[1:]):
          matcher = None
          ds = op_args
        else:
          matcher = op_args[0]
          ds = op_args[1:]
        if ds:
          ds = ensure_desktop_names(ds)
        mimeo.modify_associations(op, matcher, ds)

  if pargs.update:
    mimeo.update_mimeinfo_caches()



  if pargs.mimetype:
    if pargs.args:
      a_to_b = mimeo.args_to_mimetypes(
        pargs.args,
        at_least_one=True,
        first_only=(not pargs.show_all)
      )
      b_by_a = modify_and_collect(a_to_b, swap=pargs.swap)
      print_collection(
        b_by_a,
        order=(None if pargs.swap else pargs.args),
        sort_a=True
      )
    else:
      for x in sorted(mimeo.known_mimetypes()):
        print(x)



  elif pargs.desktop:
    if pargs.args:
      if pargs.desktop:
        a_to_b = mimeo.args_to_desktop_paths(
          pargs.args,
          at_least_one=True,
          first_only=(not pargs.show_all),
          only_existing=True
        )
        f = None if pargs.full_path else os.path.basename
        b_by_a = modify_and_collect(a_to_b, fb=f, swap=pargs.swap)
        print_collection(
          b_by_a,
          order=(None if pargs.swap else pargs.args),
          sort_a=True
        )
    else:
      if pargs.full_path:
        for p in mimeo.desktop_paths(sort_per_dir=True):
          print(p)
      else:
        ps = set(os.path.basename(p) for p in mimeo.desktop_paths())
        for p in sorted(ps):
          print(p)



  elif pargs.finddesk:
    if pargs.args:
      ds = list(os.path.basename(d) for d in ensure_desktop_names(pargs.args))
      for d in mimeo.search_desktop_paths(ds, first_only=False):
        print(d)
    else:
      for p in mimeo.desktop_paths(sort_per_dir=True):
        print(p)



  elif pargs.mime2desk:
    ms = pargs.args if pargs.args else mimeo.known_mimetypes()
    f = None if pargs.full_path else os.path.basename
    a_to_b = mimeo.mimetypes_to_desktop_paths(
      ms,
      at_least_one=True,
      first_only=(not pargs.show_all),
      only_existing=True
    )
    b_by_a = modify_and_collect(a_to_b, fb=f, swap=pargs.swap)
    print_collection(
      b_by_a,
      order=(None if pargs.swap else pargs.args),
      sort_a=True,
      sort_b=True
    )



  elif pargs.app2desk:
    a_to_b = mimeo.executables_to_desktop_paths(exes=pargs.args)
    f = None if pargs.full_path else os.path.basename
    b_by_a = modify_and_collect(a_to_b, fa=f, fb=f, swap=pargs.swap)
    print_collection(
      b_by_a,
      order=(None if pargs.swap else pargs.args),
      sort_a=True,
      sort_b=True
    )



  elif pargs.desk2field:
    ds = list(os.path.basename(d) for d in ensure_desktop_names(pargs.args))
    f = None if pargs.full_path else os.path.basename

    a_to_b = mimeo.desktop_paths_to_desktop_fields(
      pargs.desk2field, ds=ds, first_only=False
    )
    b_by_a = modify_and_collect(a_to_b, fa=f, swap=pargs.swap)
    print_collection(
      b_by_a,
      order=(None if pargs.swap else ds),
      sort_a=True,
      sort_b=True
    )



  elif pargs.mimeapps_list:
    for path in mimeo.mimeapps_list_paths():
      if os.path.exists(path):
        print(path)



  else:
    first_only = not (pargs.command and pargs.show_all)
    for c in mimeo.args_to_cmds(
      pargs.args,
      first_only=first_only
    ):
      if pargs.cmd_prefix:
        logging.debug('prepending arguments: {}'.format(extra_args))
        c = pargs.cmd_prefix + c
      if pargs.command:
        print(quote_cmd(c))
      else:
        run_cmd(c, quiet=pargs.quiet)


if __name__ == '__main__':
  logging.basicConfig(
    format='%(levelname)s: %(message)s',
    level=logging.DEBUG if ('--debug' in sys.argv[1:]) else logging.WARNING
  )
  try:
    main()
  except (KeyboardInterrupt, BrokenPipeError):
    pass
