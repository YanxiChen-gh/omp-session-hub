#!/bin/sh
# Install an immutable revision as a dependency-free Python zip application.
set -eu
revision=""
source_dir=""
prefix="${HOME}/.local/share/omp-session-hub"
checksum=""
usage() {
    echo 'Usage: install.sh --revision <40-hex-commit> [--source <checkout>] [--prefix <directory>] [--sha256 <archive-sha256>]' >&2
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --revision|--source|--prefix|--sha256)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            case "$1" in
                --revision) revision=$2 ;;
                --source) source_dir=$2 ;;
                --prefix) prefix=$2 ;;
                --sha256) checksum=$2 ;;
            esac
            shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
case "$revision" in ''|*[!0-9a-f]*) usage; exit 2 ;; esac
[ "${#revision}" -eq 40 ] || { usage; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo 'Python 3.9 or newer is required.' >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo 'Python 3.9 or newer is required.' >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
if [ -z "$source_dir" ]; then
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        "https://codeload.github.com/YanxiChen-gh/omp-session-hub/tar.gz/$revision" -o "$work/source.tar.gz"
    if [ -n "$checksum" ]; then
        python3 - "$work/source.tar.gz" "$checksum" <<'PY'
import hashlib
import re
import sys
from pathlib import Path
expected = sys.argv[2]
if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest() != expected:
    raise SystemExit("Archive checksum mismatch")
PY
    fi
    tar -xzf "$work/source.tar.gz" -C "$work"
    source_dir="$work/omp-session-hub-$revision"
fi
python3 - "$source_dir" "$prefix" "$revision" <<'PY'
import os
import shutil
import sys
import tempfile
import uuid
import zipapp
from pathlib import Path

source = Path(sys.argv[1]).resolve(strict=True)
prefix = Path(sys.argv[2]).expanduser().absolute()
revision = sys.argv[3]
if not (source / "omp_hub" / "__main__.py").is_file():
    raise SystemExit("Source does not contain omp-session-hub")
if prefix.is_symlink():
    raise SystemExit("Installation prefix must not be a symlink")
prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
versions = prefix / "versions"
if versions.is_symlink():
    raise SystemExit("Version directory must not be a symlink")
versions.mkdir(mode=0o700, exist_ok=True)
version = versions / revision
app = version / "omp-session-hub.pyz"
if version.is_symlink():
    raise SystemExit("Version directory must not be a symlink")
if not version.exists():
    stage = Path(tempfile.mkdtemp(prefix=".install-", dir=versions))
    try:
        package = stage / "package"
        package.mkdir()
        shutil.copytree(source / "omp_hub", package / "omp_hub", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        zipapp.create_archive(package, stage / "omp-session-hub.pyz", interpreter="/usr/bin/env python3", main="omp_hub.__main__:entrypoint", compressed=True)
        shutil.rmtree(package)
        os.chmod(stage / "omp-session-hub.pyz", 0o755)
        os.rename(stage, version)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
if app.is_symlink() or not app.is_file():
    raise SystemExit("Existing version is not a valid installation")

def link(target, path):
    if path.exists() and not path.is_symlink():
        raise SystemExit("Refusing to replace existing user file: " + str(path))
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    temporary.symlink_to(target)
    os.replace(temporary, path)

bin_dir = Path.home() / ".local" / "bin"
bin_dir.mkdir(parents=True, exist_ok=True)
link(version, prefix / "current")
link(app, bin_dir / "omp-session-hub")
print("Installed " + str(bin_dir / "omp-session-hub") + " (" + revision + ")")
PY
