"""Helpers shared by the v2 tests (handoff, rollover)."""
from test_autoswitch import ID, ID2, _read_json, _read_jsonl, _write_json, fake_clipimg  # noqa: F401


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _stop(srv):
    srv.shutdown()
    srv.server_close()
