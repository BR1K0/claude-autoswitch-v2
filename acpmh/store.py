"""Append-only, hash-chained event journal plus fact table, in SQLite.

Large bodies (tool output, diffs) live in blobs/<sha256> and the event keeps
a reference, so the journal stays small and every summary can point back to
the original evidence.
"""
import hashlib
import json
import os
import sqlite3
import time

SCHEMA_VERSION = 1

# Knowledge states for facts. Never collapse these into one confidence score.
FACT_STATES = ("observed", "stated", "claimed", "verified", "superseded",
               "contradicted", "unknown")
INSTRUCTION_KINDS = ("user_prompt", "agent_message")
IMAGE_KINDS = ("screenshot", "tool_image")
TEXT_KINDS = INSTRUCTION_KINDS + ("assistant_text", "notification") + IMAGE_KINDS
FACT_KINDS = ("requirement", "constraint", "decision", "rejected", "task",
              "bug", "fact", "open_question")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL,
  source_key TEXT UNIQUE NOT NULL,
  ts TEXT, session_id TEXT, model TEXT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  body_ref TEXT,
  paths TEXT,
  tokens INTEGER DEFAULT 0,
  prev_hash TEXT, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
  fact_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL,
  source_seqs TEXT NOT NULL, created_seq INTEGER,
  superseded_by INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY, ts REAL, purpose TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER,
  cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS capture_failures (
  id INTEGER PRIMARY KEY, ts REAL, source_key TEXT, error TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY, start_seq INTEGER, end_seq INTEGER, text TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
  ref UNINDEXED, kind UNINDEXED, text
);
"""


def est_tokens(text):
    """Cheap token estimate (about 4 chars per token)."""
    return (len(text) + 3) // 4


class Store:
    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "blobs"), exist_ok=True)
        self.db = sqlite3.connect(os.path.join(root, "journal.db"))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("last_incorporated_seq", "0")
        os.chmod(root, 0o700)

    # --- meta -------------------------------------------------------------
    def get_meta(self, key, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    # --- blobs ------------------------------------------------------------
    def put_blob(self, text):
        data = text.encode("utf-8", "replace")
        h = hashlib.sha256(data).hexdigest()
        path = os.path.join(self.root, "blobs", h)
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        return h

    def put_file(self, data, ext):
        """Store binary data (an image) as blobs/<sha256>.<ext>; returns that name."""
        name = "%s.%s" % (hashlib.sha256(data).hexdigest(), ext)
        path = self.blob_path(name)
        if not os.path.exists(path):
            with open(path + ".tmp", "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(path + ".tmp", path)
        return name

    def blob_path(self, name):
        return os.path.join(self.root, "blobs", name)

    def get_blob(self, ref):
        with open(os.path.join(self.root, "blobs", ref), "rb") as f:
            return f.read().decode("utf-8", "replace")

    # --- events -----------------------------------------------------------
    def last_seq(self):
        r = self.db.execute("SELECT MAX(seq) FROM events").fetchone()
        return r[0] or 0

    def append(self, source_key, kind, status, summary, body=None, ts=None,
               session_id=None, model=None, paths=None):
        """Append one event. Idempotent on source_key: returns (seq, created)."""
        r = self.db.execute("SELECT seq FROM events WHERE source_key=?",
                            (source_key,)).fetchone()
        if r:
            return r[0], False
        body_ref = self.put_blob(body) if body else None
        tokens = est_tokens(summary) + (est_tokens(body) if body else 0)
        try:
            with self.db:  # one transaction: event + index row
                prev = self.db.execute(
                    "SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                prev_hash = prev[0] if prev else ""
                seq = self.last_seq() + 1
                payload = json.dumps([seq, source_key, kind, status, summary,
                                      body_ref, ts, prev_hash], sort_keys=True)
                h = hashlib.sha256(payload.encode()).hexdigest()
                event_id = "evt_%06d" % seq
                self.db.execute(
                    "INSERT INTO events (seq,event_id,source_key,ts,session_id,model,"
                    "kind,status,summary,body_ref,paths,tokens,prev_hash,hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (seq, event_id, source_key, ts, session_id, model, kind, status,
                     summary, body_ref, json.dumps(paths or []), tokens, prev_hash, h))
                idx = summary + ("\n" + body[:4000] if body else "")
                self.db.execute("INSERT INTO search VALUES (?,?,?)",
                                (event_id, kind, idx))
            return seq, True
        except Exception as e:  # never drop silently: keep it for retry
            with self.db:
                self.db.execute(
                    "INSERT INTO capture_failures (ts,source_key,error,payload) "
                    "VALUES (?,?,?,?)",
                    (time.time(), source_key, repr(e),
                     json.dumps({"kind": kind, "summary": summary})))
            raise

    def events(self, after=0, upto=None, kinds=None):
        q = "SELECT * FROM events WHERE seq>?"
        args = [after]
        if upto is not None:
            q += " AND seq<=?"
            args.append(upto)
        if kinds:
            q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += list(kinds)
        return self.db.execute(q + " ORDER BY seq", args).fetchall()

    def event(self, seq_or_id):
        col = "event_id" if str(seq_or_id).startswith("evt_") else "seq"
        return self.db.execute("SELECT * FROM events WHERE %s=?" % col,
                               (seq_or_id,)).fetchone()

    def verify_chain(self):
        """Return list of problems: sequence gaps or hash mismatches."""
        problems, prev_hash, expect = [], "", 1
        for e in self.db.execute("SELECT * FROM events ORDER BY seq"):
            if e["seq"] != expect:
                problems.append("gap before seq %d" % e["seq"])
            payload = json.dumps([e["seq"], e["source_key"], e["kind"], e["status"],
                                  e["summary"], e["body_ref"], e["ts"], prev_hash],
                                 sort_keys=True)
            if e["prev_hash"] != prev_hash or \
                    hashlib.sha256(payload.encode()).hexdigest() != e["hash"]:
                problems.append("hash mismatch at seq %d" % e["seq"])
            prev_hash, expect = e["hash"], e["seq"] + 1
        return problems

    # --- facts ------------------------------------------------------------
    def add_fact(self, kind, text, status, source_seqs, note=None):
        if kind not in FACT_KINDS:
            kind = "fact"
        if status not in FACT_STATES:
            status = "claimed"
        with self.db:
            cur = self.db.execute(
                "INSERT INTO facts (kind,text,status,source_seqs,created_seq,note) "
                "VALUES (?,?,?,?,?,?)",
                (kind, text, status, json.dumps(source_seqs),
                 max(source_seqs) if source_seqs else None, note))
            fid = cur.lastrowid
            self.db.execute("INSERT INTO search VALUES (?,?,?)",
                            ("fact_%d" % fid, kind, text))
        return fid

    def supersede(self, fact_id, by_fact_id):
        with self.db:
            self.db.execute("UPDATE facts SET status='superseded', superseded_by=? "
                            "WHERE fact_id=?", (by_fact_id, fact_id))

    def active_facts(self, kinds=None):
        q = "SELECT * FROM facts WHERE status NOT IN ('superseded')"
        args = []
        if kinds:
            q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args = list(kinds)
        return self.db.execute(q + " ORDER BY fact_id", args).fetchall()

    def add_segment(self, start_seq, end_seq, text):
        with self.db:
            self.db.execute("INSERT INTO segments (start_seq,end_seq,text) "
                            "VALUES (?,?,?)", (start_seq, end_seq, text))

    def segments(self):
        return self.db.execute("SELECT * FROM segments ORDER BY start_seq").fetchall()

    def cited_seqs(self):
        out = set()
        for (s,) in self.db.execute("SELECT source_seqs FROM facts"):
            out.update(json.loads(s))
        return out

    # --- usage ------------------------------------------------------------
    def record_usage(self, purpose, model, usage):
        with self.db:
            self.db.execute(
                "INSERT INTO usage (ts,purpose,model,input_tokens,output_tokens,"
                "cache_read,cache_write) VALUES (?,?,?,?,?,?,?)",
                (time.time(), purpose, model, usage.input_tokens, usage.output_tokens,
                 getattr(usage, "cache_read_input_tokens", 0) or 0,
                 getattr(usage, "cache_creation_input_tokens", 0) or 0))

    # --- search -----------------------------------------------------------
    def search(self, query, limit=8):
        terms = [t for t in "".join(c if c.isalnum() or c in "_-./" else " "
                                    for c in query).split() if len(t) > 1]
        if not terms:
            return []
        fts = " OR ".join('"%s"' % t.replace('"', "") for t in terms)
        return self.db.execute(
            "SELECT ref, kind, snippet(search, 2, '[', ']', ' … ', 24) AS snip "
            "FROM search WHERE search MATCH ? ORDER BY bm25(search) LIMIT ?",
            (fts, limit)).fetchall()
