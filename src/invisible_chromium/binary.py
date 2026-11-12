"""Binary discovery, verification, and public GitHub Release installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SEAL_PATH = PACKAGE_DIR / "seal.json"

DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_S = (2.0, 8.0)
TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})


def load_seal(seal_path: str | os.PathLike[str] | None = None) -> dict:
    target = (
        Path(seal_path)
        if seal_path
        else Path(os.environ.get("INVISIBLE_CHROMIUM_SEAL_FILE") or DEFAULT_SEAL_PATH)
    )
    return json.loads(target.read_text(encoding="utf-8"))


def cache_root() -> Path:
    override = os.environ.get("INVISIBLE_CHROMIUM_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".cache")
    return base / "invisible-chromium"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_verified_archive(
    archive: Path,
    *,
    expected_sha256: str,
    expected_size: int | None,
    dest_dir: Path,
) -> Path:
    actual_size = archive.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise RuntimeError(
            f"Chromium archive size mismatch for {archive}: "
            f"expected {expected_size} bytes, got {actual_size} bytes"
        )
    actual_sha = _sha256_file(archive)
    if actual_sha != expected_sha256:
        raise RuntimeError(
            f"Chromium archive SHA256 mismatch for {archive}: "
            f"expected {expected_sha256}, got {actual_sha}"
        )
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}-extract-", dir=str(dest_dir.parent))
    )
    os.chmod(tmp_dir, 0o755)
    try:
        with tarfile.open(archive, mode="r:gz") as tf:
            for member in tf.getmembers():
                member_path = (tmp_dir / member.name).resolve()
                if not str(member_path).startswith(str(tmp_dir.resolve())):
                    raise RuntimeError(f"Unsafe tar member path: {member.name}")
            tf.extractall(tmp_dir, filter="fully_trusted")
        marker = tmp_dir / ".verified-seal-sha256"
        marker.write_text(expected_sha256 + "\n", encoding="utf-8")
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        tmp_dir.rename(dest_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return dest_dir


def _is_transient_download_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_CODES
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _download_public_release_archive(url: str, dest_file: Path) -> None:
    asset_name = url.rsplit("/", 1)[-1]
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "invisible-chromium"}
            )
            with urllib.request.urlopen(req, timeout=600) as resp, dest_file.open(
                "wb"
            ) as out_f:
                shutil.copyfileobj(resp, out_f, length=1024 * 1024)
            return
        except Exception as exc:
            if attempt == DOWNLOAD_ATTEMPTS or not _is_transient_download_error(exc):
                raise
            wait_s = DOWNLOAD_BACKOFF_S[attempt - 1]
            print(
                f"  {asset_name}: attempt {attempt} of {DOWNLOAD_ATTEMPTS} failed "
                f"({exc}); retrying in {wait_s:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait_s)


def ensure_chromium_binary(
    binary_path: str | os.PathLike[str] | None = None,
    *,
    seal_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve or install the verified patched Chromium executable from GitHub Releases."""
    if binary_path is not None:
        resolved = Path(binary_path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Chromium binary not found: {resolved}")
        return resolved

    env_bin = os.environ.get("INVISIBLE_CHROMIUM_BINARY")
    if env_bin:
        resolved = Path(env_bin).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(
                f"INVISIBLE_CHROMIUM_BINARY points to missing file: {resolved}"
            )
        return resolved

    sys_key = f"{platform.system().lower()}-{platform.machine().lower()}"
    if sys_key == "linux-amd64":
        sys_key = "linux-x86_64"

    seal = load_seal(seal_path)
    asset = seal.get("assets", {}).get(sys_key)
    if not asset:
        raise RuntimeError(
            f"No Chromium binary asset recorded in seal for platform {sys_key}"
        )

    tag = seal["tag"]
    expected_sha = asset["sha256"]
    expected_size = asset.get("size_bytes")
    entry_rel = asset.get("entry_rel", "chrome")

    install_dir = cache_root() / tag
    marker = install_dir / ".verified-seal-sha256"
    entry = install_dir / entry_rel
    if (
        entry.is_file()
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == expected_sha
    ):
        return entry

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="chromium-download-",
        suffix=".tar.gz",
        dir=str(install_dir.parent),
        delete=False,
    ) as tmp_f:
        tmp_archive = Path(tmp_f.name)
    try:
        _download_public_release_archive(asset["url"], tmp_archive)
        _extract_verified_archive(
            tmp_archive,
            expected_sha256=expected_sha,
            expected_size=expected_size,
            dest_dir=install_dir,
        )
    finally:
        tmp_archive.unlink(missing_ok=True)

    return install_dir / entry_rel
