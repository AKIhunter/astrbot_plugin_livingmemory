#!/usr/bin/env python3
"""Verify package identity, allowlist and byte agreement with the source commit."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from build_release import allowed, git

parser = argparse.ArgumentParser()
parser.add_argument("package",type=Path)
args = parser.parse_args()
assert args.package.stat().st_size < 16*1024*1024
with zipfile.ZipFile(args.package) as z:
    names = z.namelist()
    assert len(names)==len(set(names)), "duplicate ZIP member"
    manifest = json.loads(z.read("release-build.json"))
    assert set(names)==set(manifest["files"])|{"release-build.json"}
    for name,digest in manifest["files"].items():
        assert allowed(name), name
        data = z.read(name)
        assert hashlib.sha256(data).hexdigest()==digest, name
        assert git("show",f'{manifest["commit"]}:{name}')==data, name
    assert git("rev-parse",manifest["commit"]+"^{tree}").decode().strip()==manifest["tree"]
print(json.dumps({"passed":True,"version":manifest["version"],"commit":manifest["commit"],
                  "tree":manifest["tree"],"files":len(names),
                  "sha256":hashlib.sha256(args.package.read_bytes()).hexdigest()},indent=2))
