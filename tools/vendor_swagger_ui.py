"""Preparation-only: vendor official Swagger UI assets, verifying npm integrity.

Never imported by the API. Run on an approved connected preparation machine.
"""

import argparse
import base64
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path


def download(url: str, limit: int):
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Download exceeds preparation limit")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="latest")
    args = parser.parse_args()
    if not all(c.isalnum() or c in ".-" for c in args.version):
        raise ValueError("Invalid Swagger UI version")
    metadata = json.loads(download("https://registry.npmjs.org/swagger-ui-dist/" + args.version, 1024 * 1024))
    url = metadata["dist"]["tarball"]
    if not url.startswith("https://registry.npmjs.org/swagger-ui-dist/-/"):
        raise ValueError("Unexpected package source")
    package = download(url, 32 * 1024 * 1024)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(package).digest()).decode("ascii")
    if integrity != metadata["dist"]["integrity"]:
        raise ValueError("Package integrity mismatch")
    destination = Path(__file__).resolve().parents[1] / "app" / "static" / "swagger-ui"
    destination.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        # Fixed filenames only; never extract paths supplied by the package.
        for name in ("swagger-ui-bundle.js", "swagger-ui.css", "LICENSE", "NOTICE"):
            try:
                member = archive.getmember("package/" + name)
            except KeyError:
                if name == "NOTICE":
                    continue
                raise
            if not member.isfile() or member.size > 8 * 1024 * 1024:
                raise ValueError("Invalid asset")
            with archive.extractfile(member) as asset:
                data = asset.read()
            (destination / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
    manifest = {"package": "swagger-ui-dist", "version": metadata["version"], "source": url,
                "integrity": integrity, "sha256": hashes}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Vendored Swagger UI " + metadata["version"] + " with verified integrity")


if __name__ == "__main__":
    main()
