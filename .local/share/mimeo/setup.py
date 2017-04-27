#!/usr/bin/env python3

from distutils.core import setup
import time

setup(
  name='''Mimeo''',
  version=time.strftime('%Y.%m.%d.%H.%M.%S', time.gmtime(1486684127)),
  description='''Open files by MIME-type or file name using regular expressions.''',
  author='''Xyne''',
  author_email='''ac xunilhcra enyx, backwards''',
  url='''http://xyne.archlinux.ca/projects/mimeo''',
  py_modules=['''Mimeo'''],
)
