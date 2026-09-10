"""Test original methods without importing Discord/OCR or logging into accounts."""
import ast
import atexit
import asyncio
import copy
import json
import os
from pathlib import Path
import random
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix='lazyfarmers-tests-')
os.environ['LAZYFARMERS_DATA_ROOT'] = TEMP.name
atexit.register(TEMP.cleanup)


def extract(path, names, env=None, class_name=None):
    scope = dict(os=os, asyncio=asyncio, copy=copy, json=json, time=time, random=random)
    scope.update(env or {})
    tree = ast.parse((ROOT / path).read_text())
    nodes = tree.body
    if class_name:
        original = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == class_name)
        nodes = original.body
    nodes = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (names is None or n.name in names)]
    for node in nodes:
        node.decorator_list = [d for d in node.decorator_list if isinstance(d, ast.Name) and d.id in ('property', 'staticmethod', 'classmethod')]
    if class_name:
        nodes = [ast.ClassDef(name=class_name, bases=[], keywords=[], body=nodes, decorator_list=[])]
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(ROOT / path), 'exec'), scope)
    return scope[class_name] if class_name else scope
