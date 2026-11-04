"""Static AST tripwire over candidate-editable files (Momus re-review #2, item E/F)."""
import pytest

from omo_grok.candidate_scan import scan_source

BAD = {
    "os_exit": "import os\nos._exit(0)\n",
    "os_exit_alias": "import os as o\no._exit(0)\n",
    "from_os_exit": "from os import _exit\n_exit(0)\n",
    "sys_exit": "import sys\nsys.exit(0)\n",
    "raise_systemexit": "raise SystemExit(0)\n",
    "unittest_import": "import unittest\n",
    "unittest_from": "from unittest import case\n",
    "builtins_mutation": "import builtins\nbuiltins.len = lambda x: 0\n",
    "dunder_builtins": "__builtins__['len'] = None\n",
    "sys_modules": "import sys\nsys.modules['unittest'] = None\n",
    "ctypes": "import ctypes\n",
    "importlib": "import importlib\n",
    "gc": "import gc\n",
    "inspect": "import inspect\n",
    "frame_hack": "def f():\n    return f.__globals__\n",
    "class_hack": "x = ().__class__.__bases__[0].__subclasses__()\n",
    "setattr": "setattr(object, 'x', 1)\n",
    "exec": "exec('1')\n",
    "eval": "eval('1')\n",
    "compile": "compile('1', 'x', 'exec')\n",
    "dunder_import": "__import__('os')\n",
    "eq_override": "class A:\n    def __eq__(self, o):\n        return True\n",
    "int_subclass": "class A(int):\n    pass\n",
    "metaclass": "class M(type):\n    pass\nclass A(metaclass=M):\n    pass\n",
    "global_stmt": "x = 1\ndef f():\n    global x\n    x = 2\n",
    "star_import": "from os import *\n",
    "open_call": "open('/etc/passwd')\n",
    "syntax_error": "def (:\n",
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_forbidden_constructs_rejected(name):
    assert scan_source(BAD[name], "calc/__init__.py", {}), name


GOOD = {
    "real_fix": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
    "plain_class": "class Point:\n    def __init__(self, x):\n        self.x = x\n\n    def __repr__(self):\n        return 'P'\n",
    "math_import": "import math\n\n\ndef area(r):\n    return math.pi * r * r\n",
    "docstring": '"""notes"""\n',
}


@pytest.mark.parametrize("name", sorted(GOOD))
def test_ordinary_code_accepted(name):
    assert scan_source(GOOD[name], "calc/__init__.py", {}) == [], name


def test_rules_are_configurable_but_fail_closed_by_default():
    src = "import gc\n"
    assert scan_source(src, "calc/x.py", {})
    assert scan_source(src, "calc/x.py", {"allow_imports": ["gc"]}) == []
    assert scan_source("import json\n", "calc/x.py", {"forbid_imports": ["json"]})
    eq = BAD["eq_override"]
    assert scan_source(eq, "calc/x.py", {"allow_dunder_overrides": ["__eq__"]}) == []
