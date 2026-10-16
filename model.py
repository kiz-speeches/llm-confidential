"""The same allowlist and SHA-256 check is used at build time and startup."""
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

LOCK = Path(__file__).with_name("model.lock.json")


def prepare(root: Path, download=False, lock_path=LOCK):
    lock = json.loads(lock_path.read_text())
    root.mkdir(parents=True, exist_ok=True)
    for name, expected in lock["files"].items():
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("Invalid model filename")
        target = root / name
        if target.is_symlink():
            raise ValueError("Model symlinks are forbidden")
        if download and not target.exists():
            url = f'https://huggingface.co/{lock["repository"]}/resolve/{lock["revision"]}/{name}'
            partial = root / (name + ".partial")
            try:
                with urllib.request.urlopen(url, timeout=300) as source, partial.open("wb") as dest:
                    while chunk := source.read(8 * 1024 * 1024):
                        dest.write(chunk)
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        digest = hashlib.sha256()
        with target.open("rb") as source:
            while chunk := source.read(8 * 1024 * 1024):
                digest.update(chunk)
        if target.stat().st_size != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError(f"Model integrity check failed: {name}")
    if {p.name for p in root.iterdir()} != set(lock["files"]):
        raise ValueError("Unexpected model files")


if __name__ == "__main__":
    prepare(Path(sys.argv[1]), download="--download" in sys.argv[2:])
