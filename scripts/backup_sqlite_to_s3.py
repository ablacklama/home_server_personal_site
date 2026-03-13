#!/usr/bin/env python3
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse


def _get_sqlite_path(database_url: str) -> pathlib.Path:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise ValueError("DATABASE_URL must start with sqlite://")

    # sqlite:///./data/foo.sqlite3 -> parsed.path is /./data/foo.sqlite3
    db_path = parsed.path
    if db_path.startswith("/") and db_path.startswith("//") is False:
        # For relative paths urlparse keeps a leading slash.
        db_path = db_path[1:]
    if not db_path:
        raise ValueError("SQLite DATABASE_URL has no path")
    return pathlib.Path(db_path)


def main() -> int:
    database_url = os.getenv(
        "DATABASE_URL", "sqlite:///./data/personal_site.sqlite3"
    ).strip()
    s3_prefix = os.getenv("BACKUP_S3_PREFIX", "").strip().rstrip("/")
    aws_profile = os.getenv("AWS_PROFILE", "").strip()

    if not s3_prefix:
        print("BACKUP_S3_PREFIX is required (e.g. s3://bucket/prefix)", file=sys.stderr)
        return 2

    if shutil.which("aws") is None:
        print("aws CLI is required to upload backups (install awscli)", file=sys.stderr)
        return 2

    db_path = _get_sqlite_path(database_url)
    if not db_path.exists():
        print(f"SQLite DB not found: {db_path}", file=sys.stderr)
        return 2

    ts = time.strftime("%Y-%m-%d_%H%M%S")
    backup_name = f"personal_site_{ts}.sqlite3"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir) / backup_name

        src = sqlite3.connect(str(db_path))
        try:
            dst = sqlite3.connect(str(tmp_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        s3_uri = f"{s3_prefix}/{backup_name}"
        cmd = ["aws"]
        if aws_profile:
            cmd += ["--profile", aws_profile]
        cmd += ["s3", "cp", str(tmp_path), s3_uri]

        subprocess.run(cmd, check=True)
        print(f"Uploaded backup to {s3_uri}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
