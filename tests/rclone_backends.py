"""Throwaway rclone backends for the live tests: a local folder, SFTP and S3 in Docker on 127.0.0.1.

Used by tests/test_rclone_live.py as a fixture, and by hand:

    uv run python tests/rclone_backends.py up     # starts both containers, prints the rclone config path
    uv run python tests/rclone_backends.py down   # removes every container these tests started

Containers carry the label `minuta-test=rclone`, publish only on 127.0.0.1 and are removed on exit.
The rclone config is a temporary file; the user's own rclone config is never read or written.

rclone is taken from $MINUTA_TEST_RCLONE, then PATH, then ~/.cache/minuta-test/rclone (download the
official zip from https://downloads.rclone.org there; no root needed).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LABEL = "minuta-test=rclone"
SFTP_IMAGE = os.environ.get("MINUTA_TEST_SFTP_IMAGE", "atmoz/sftp:latest")
# MinIO stopped publishing public images; SeaweedFS speaks the same S3 API.
S3_IMAGE = os.environ.get("MINUTA_TEST_S3_IMAGE", "chrislusf/seaweedfs:latest")
BUCKET = "minuta-test"


def find_rclone() -> str | None:
    for candidate in (os.environ.get("MINUTA_TEST_RCLONE"), shutil.which("rclone"),
                      str(Path.home() / ".cache" / "minuta-test" / "rclone")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rclone(binary: str, config: Path, *args: str, check: bool = True, timeout: int = 60):
    return subprocess.run([binary, "--config", str(config), "--ask-password=false", *args], capture_output=True,
                          text=True, timeout=timeout, check=check)


def wait_until(fn, seconds: float, what: str) -> None:
    end = time.time() + seconds
    last = None
    while time.time() < end:
        try:
            if fn():
                return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"{what} did not come up in {seconds} s ({last})")


class Backends:
    """Starts what was asked for and writes one rclone config with a remote per backend."""

    def __init__(self, binary: str, root: Path | None = None):
        self.binary = binary
        self.root = Path(root or tempfile.mkdtemp(prefix="minuta-rclone-"))
        self.config = self.root / "rclone.conf"
        self.containers: list[str] = []
        self.sections: dict[str, dict[str, str]] = {}
        self.base: dict[str, str] = {}  # remote -> base folder for the tests
        self.tag = secrets.token_hex(4)

    def obscure(self, value: str) -> str:
        return subprocess.run([self.binary, "obscure", value], capture_output=True, text=True,
                              check=True).stdout.strip()

    def write(self) -> None:
        lines = []
        for name, opts in self.sections.items():
            lines.append(f"[{name}]")
            lines += [f"{k} = {v}" for k, v in opts.items()]
            lines.append("")
        self.config.write_text("\n".join(lines))
        self.config.chmod(0o600)

    def run(self, *args: str, check: bool = True):
        return rclone(self.binary, self.config, *args, check=check)

    def _docker(self, name: str, *args: str) -> None:
        subprocess.run(["docker", "run", "-d", "--rm", "--label", LABEL, "--name", name, *args], check=True,
                       capture_output=True, text=True)
        self.containers.append(name)

    # --- backends ----------------------------------------------------------------------
    def local(self) -> str:
        folder = self.root / "local-remote"
        folder.mkdir()
        self.sections["loc"] = {"type": "alias", "remote": str(folder)}
        self.base["loc"] = "minuta"
        self.write()
        return "loc"

    def sftp(self) -> str:
        port, password = free_port(), secrets.token_urlsafe(12)
        self._docker(f"minuta-test-sftp-{self.tag}", "-p", f"127.0.0.1:{port}:22", SFTP_IMAGE,
                     f"minuta:{password}:::upload")
        common = {"type": "sftp", "host": "127.0.0.1", "port": str(port), "user": "minuta",
                  "shell_type": "none", "md5sum_command": "none", "sha1sum_command": "none"}
        self.sections["sftp"] = {**common, "pass": self.obscure(password)}
        self.sections["sftpbad"] = {**common, "pass": self.obscure("wrong-" + password)}
        self.base["sftp"] = "upload/minuta"
        self.write()
        wait_until(lambda: self.run("lsjson", "sftp:upload", check=False).returncode == 0, 60, "sftp")
        return "sftp"

    def s3(self) -> str:
        port = free_port()
        key, secret = "minuta" + secrets.token_hex(4), secrets.token_hex(16)
        cfg_dir = self.root / "s3cfg"
        cfg_dir.mkdir()
        identity = {"name": "minuta", "credentials": [{"accessKey": key, "secretKey": secret}],
                    "actions": ["Admin", "Read", "Write", "List", "Tagging"]}
        (cfg_dir / "s3.json").write_text(json.dumps({"identities": [identity]}))
        cfg_dir.chmod(0o755)
        (cfg_dir / "s3.json").chmod(0o644)
        self._docker(f"minuta-test-s3-{self.tag}", "-p", f"127.0.0.1:{port}:8333", "-v", f"{cfg_dir}:/cfg:ro",
                     S3_IMAGE, "server", "-s3", "-s3.config=/cfg/s3.json", "-ip.bind=0.0.0.0")
        common = {"type": "s3", "provider": "SeaweedFS", "endpoint": f"http://127.0.0.1:{port}",
                  "region": "us-east-1", "force_path_style": "true"}
        self.sections["s3"] = {**common, "access_key_id": key, "secret_access_key": secret}
        self.sections["s3bad"] = {**common, "access_key_id": key, "secret_access_key": "wrong" + secret[5:]}
        self.base["s3"] = f"{BUCKET}/minuta"
        self.write()
        wait_until(lambda: self.run("mkdir", f"s3:{BUCKET}", check=False).returncode == 0
                   and self.run("lsjson", f"s3:{BUCKET}", check=False).returncode == 0, 90, "s3")
        return "s3"

    def stop(self, name: str) -> None:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    def close(self) -> None:
        for name in self.containers:
            self.stop(name)
        self.containers.clear()
        shutil.rmtree(self.root, ignore_errors=True)


def down() -> None:
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={LABEL}"], capture_output=True,
                         text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)
    print(f"removed {len(ids)} container(s)")


def main(argv: list[str]) -> int:
    action = argv[0] if argv else "up"
    if action == "down":
        down()
        return 0
    binary = find_rclone()
    if not binary or not docker_ok():
        print("needs rclone and a running docker", file=sys.stderr)
        return 1
    b = Backends(binary)
    for start in (b.local, b.sftp, b.s3):
        start()
    print(f"rclone config: {b.config}\ncontainers: {', '.join(b.containers)}\n"
          f"stop with: uv run python tests/rclone_backends.py down")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
