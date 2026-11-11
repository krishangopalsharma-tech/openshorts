"""Crash-safe atomic JSON writes on Windows and POSIX."""

import json
import os
import tempfile


def dump_atomic(data, path, indent=2):
    if isinstance(data, (str, bytes, os.PathLike)) and not isinstance(path, (str, bytes, os.PathLike)):
        data, path = path, data
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
