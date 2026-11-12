"""Binary discovery, verification, and release archive installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SEAL_PATH = PACKAGE_DIR / "seal.json"


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


def _extract_verified_archive(archive: Path, expected_sha256: str, dest_dir: Path) -> Path:
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


def _github_token() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val.strip()
    gh = shutil.which("gh")
    if gh:
        try:
            res = subprocess.run(
                [gh, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except OSError:
            pass
    return None


def _download_release_archive(seal: dict, asset: dict, dest_file: Path) -> None:
    token = _github_token()
    repo = seal["repository"]
    tag = seal["tag"]
    asset_name = asset["name"]

    # Support private GitHub repositories via API release asset lookup when authenticated.
    if token:
        api_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        req = urllib.request.Request(
            api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "invisible-chromium",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                release_info = json.load(resp)
            for item in release_info.get("assets", []):
                if item.get("name") == asset_name:
                    dl_req = urllib.request.Request(
                        item["url"],
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/octet-stream",
                            "User-Agent": "invisible-chromium",
                        },
                    )
                    with urllib.request.urlopen(dl_req, timeout=600) as dl_resp, dest_file.open(
                        "wb"
                    ) as out_f:
                        shutil.copyfileobj(dl_resp, out_f, length=1024 * 1024)
                    return
        except Exception:
            pass

    headers = {"User-Agent": "invisible-chromium"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(asset["url"], headers=headers)
    with urllib.request.urlopen(req, timeout=600) as resp, dest_file.open("wb") as out_f:
        shutil.copyfileobj(resp, out_f, length=1024 * 1024)


def ensure_chromium_binary(
    binary_path: str | os.PathLike[str] | None = None,
    *,
    seal_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve or install the verified patched Chromium executable."""
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
        raise RuntimeError(f"No Chromium binary asset recorded in seal for platform {sys_key}")

    tag = seal["tag"]
    expected_sha = asset["sha256"]
    entry_rel = asset.get("entry_rel", "chrome")

    install_dir = cache_root() / tag
    marker = install_dir / ".verified-seal-sha256"
    entry = install_dir / entry_rel
    if entry.is_file() and marker.is_file() and marker.read_text(encoding="utf-8").strip() == expected_sha:
        return entry

    # Check local workspace release archive if running in the development workspace.
    if os.environ.get("INVISIBLE_CHROMIUM_NO_LOCAL_FALLBACK") != "1":
        workspace_candidates = [
            PACKAGE_DIR.parents[2] / "build" / "releases" / tag / asset["name"],
            Path.cwd() / "build" / "releases" / tag / asset["name"],
        ]
        for candidate in workspace_candidates:
            if candidate.is_file() and _sha256_file(candidate) == expected_sha:
                _extract_verified_archive(candidate, expected_sha, install_dir)
                return install_dir / entry_rel

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="chromium-download-", suffix=".tar.gz", dir=str(install_dir.parent), delete=False
    ) as tmp_f:
        tmp_archive = Path(tmp_f.name)
    try:
        _download_release_archive(seal, asset, tmp_archive)
        _extract_verified_archive(tmp_archive, expected_sha, install_dir)
    finally:
        tmp_archive.unlink(missing_ok=True)

    return install_dir / entry_rel
