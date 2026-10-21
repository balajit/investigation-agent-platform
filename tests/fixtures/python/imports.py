"""Synthetic Python fixture: static, conditional, and dynamic imports."""

import os

import a.b as c

from m import n as alias

from .local import helper

try:
    import optional_dep
except ImportError:
    optional_dep = None

import importlib

dynamic = importlib.import_module("plugin")

from . import sibling
