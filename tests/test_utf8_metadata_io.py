"""Clip metadata is written as raw UTF-8 (jsonfile.dump_atomic, ensure_ascii=False)
and now routinely carries emoji (Twitter/X captions). On Windows, open() without
an encoding reads with the ANSI codepage (cp1252), and a 20-clip movie job
failed AFTER rendering every clip with "'charmap' codec can't decode byte 0x9d".
Every json / metadata open in app.py must name its encoding."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OPEN = re.compile(r"open\(([^()]*(?:\([^()]*\))?[^()]*)\)")


def test_app_metadata_opens_name_utf8():
    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        src = f.read()
    bad = []
    for n, line in enumerate(src.splitlines(), 1):
        for m in _OPEN.finditer(line):
            args = m.group(1)
            if re.search(r"json|meta", args, re.I) and "encoding" not in args and "'rb'" not in args \
                    and '"rb"' not in args and "'wb'" not in args:
                bad.append(f"app.py:{n}: {line.strip()}")
    assert not bad, "metadata opened without encoding:\n" + "\n".join(bad)


def test_emoji_metadata_round_trips_through_app_reader(tmp_path, monkeypatch):
    """The read app.py does at job end, on a file written the way main.py writes it."""
    import json
    import jsonfile

    meta = {"shorts": [{"pov_text": "A real racer's dream became cinema 📜🔥", "start": 1, "end": 20}]}
    p = tmp_path / "x_metadata.json"
    jsonfile.dump_atomic(str(p), meta)
    raw = p.read_bytes()
    assert b"\xf0\x9f\x93\x9c" in raw            # stored as UTF-8, not \u escapes
    with open(p, "r", encoding="utf-8") as f:
        assert json.load(f) == meta
