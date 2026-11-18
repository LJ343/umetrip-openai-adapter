#!/usr/bin/env python3
"""Download exactly the reviewed official CLI, verify its pinned integrity."""
import base64
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    manifest = json.loads((ROOT / "docs/upstream/manifest.json").read_text())
    version = manifest["cli_version"]
    url = f"https://registry.npmjs.org/@fly-ai/flyai-cli/-/flyai-cli-{version}.tgz"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(1048576)
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != manifest["cli_integrity"]:
        raise SystemExit("CLI integrity mismatch; nothing installed")
    (ROOT / "runtime").mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for source, target in [("package/dist/flyai-bundle.cjs", "flyai-bundle.cjs"), ("package/package.json", "package.json")]:
            member = archive.getmember(source)
            if not member.isfile():
                raise SystemExit("Unexpected package layout")
            (ROOT / "runtime" / target).write_bytes(archive.extractfile(member).read())
    print("Official FlyAI CLI installed locally, version " + version + "; no business query executed.")


if __name__ == "__main__":
    main()
