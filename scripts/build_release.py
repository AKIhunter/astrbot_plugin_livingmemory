#!/usr/bin/env python3
"""Build an immutable plugin ZIP from a committed tree, without ambient files."""
import argparse
import hashlib
import json
import platform
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TOP = {"main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt",
       "privacy_lexicon.default.json", "README.md", "README_en.md", "README_ru.md",
       "README_zh.md", "LICENSE", "CHANGELOG.md", "logo.png"}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def allowed(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or any(
        x.startswith(".") for x in p.parts
    ):
        return name == ".github/img/Banner.png"
    if name in TOP:
        return True
    if p.parts[0] in {"core", "storage"}:
        return p.suffix in {".py", ".json"}
    if p.parts[0] == "pages":
        return p.suffix in {".html", ".css", ".js", ".json", ".txt", ".svg"}
    return name == "static/stopwords/stopwords_hit.txt"


def build(output, revision):
    commit = git("rev-parse", revision+"^{commit}").decode().strip()
    tree = git("rev-parse", commit+"^{tree}").decode().strip()
    names = sorted(n for n in git("ls-tree","-r","--name-only",commit).decode().splitlines()
                   if allowed(n))
    files = {n:git("show",f"{commit}:{n}") for n in names}
    version = next(line.split(":",1)[1].strip() for line in files["metadata.yaml"].decode().splitlines()
                   if line.startswith("version:"))
    manifest = {"version":version,"commit":commit,"tree":tree,
                "python":platform.python_version(),
                "builder_sha256":hashlib.sha256(git("show",f"{commit}:scripts/build_release.py")).hexdigest(),
                "lock_sha256":hashlib.sha256(git("show",f"{commit}:package-lock.json")).hexdigest(),
                "generator_sha256":hashlib.sha256(git("show",f"{commit}:scripts/build_dashboard.mjs")).hexdigest(),
                "files":{n:hashlib.sha256(b).hexdigest() for n,b in files.items()}}
    files["release-build.json"] = (json.dumps(manifest,sort_keys=True,indent=2)+"\n").encode()
    with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,data in sorted(files.items()):
            info = zipfile.ZipInfo(name,(1980,1,1,0,0,0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info,data,compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    assert output.stat().st_size < 16*1024*1024
    print(json.dumps({"version":version,"commit":commit,"tree":tree,
                      "files":len(files),"bytes":output.stat().st_size,
                      "sha256":hashlib.sha256(output.read_bytes()).hexdigest()},indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--revision",default="HEAD")
    args = parser.parse_args()
    assert args.output.parent.is_dir()
    build(args.output,args.revision)
