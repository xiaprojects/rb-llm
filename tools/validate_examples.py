#!/usr/bin/env python3
"""Validate every examples/*.json against the schema it names.

Each example file is {"schema": "<file>[#/$defs/...]", "description": "...", "body": {...}}.
Usage: python3 tools/validate_examples.py   (needs: pip install jsonschema)
"""
import glob
import json
import os
import sys

import jsonschema
from referencing import Registry, Resource

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://rbavionics.com/schemas/rb-llm/1/"

registry = Registry()
for path in glob.glob(os.path.join(ROOT, "schemas", "*.json")):
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.Draft202012Validator.check_schema(schema)
    registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))

failed = 0
for path in sorted(glob.glob(os.path.join(ROOT, "examples", "*.json"))):
    with open(path, encoding="utf-8") as f:
        example = json.load(f)
    validator = jsonschema.Draft202012Validator({"$ref": BASE + example["schema"]}, registry=registry)
    errors = sorted(validator.iter_errors(example["body"]), key=lambda e: list(e.path))
    name = os.path.relpath(path, ROOT)
    if errors:
        failed += 1
        print(f"FAIL {name}")
        for e in errors[:5]:
            print(f"     {'/'.join(map(str, e.path)) or '<root>'}: {e.message}")
    else:
        print(f"ok   {name}")
sys.exit(1 if failed else 0)
