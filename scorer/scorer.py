"""The scorer: a tiny local HTTP service that rates how hard a prompt is (haiku | sonnet | opus).

  python scorer.py [--port 8765] [--models DIR]

POST / {"text": "...", "input": "..."}  ->  {"label": "haiku", "probs": {"haiku": .., "sonnet": .., "opus": ..}}
GET  /health                              ->  ok

Listens on 127.0.0.1 only. Two models vote: one trained on short chat prompts (the main voice), one on
SWE-bench issues labelled by what actually solved them. The second one gets more say on long, issue-like text.
"""
import argparse
import json
import os
import pickle
import stat
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = ["haiku", "sonnet", "opus"]
MEASURED_WEIGHT = float(os.environ.get("AUTOSWITCH_MEASURED_WEIGHT", "0.2"))
LONG_TEXT = 600  # characters from which a prompt looks like a bug report rather than a chat message


def load(models_dir, name):
    with open(os.path.join(models_dir, name + ".pkl"), "rb") as f:
        model = pickle.load(f)
    return model, [list(model.classes_).index(c) for c in LABELS]


def make_handler(synthetic, measured):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply(200, b"ok", "text/plain")

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                text = body.get("text", "")[-4000:]
                (m1, i1), (m2, i2) = synthetic, measured
                w = max(MEASURED_WEIGHT, 0.5) if len(text) >= LONG_TEXT else MEASURED_WEIGHT
                probs = (1 - w) * m1.predict_proba([text])[0][i1] + w * m2.predict_proba([text])[0][i2]
                best = int(probs.argmax())
                self._reply(200, json.dumps({"label": LABELS[best], "probs": dict(zip(LABELS, probs.tolist()))}).encode())
            except Exception as e:
                print(f"score error: {e!r}", flush=True)  # goes to logs/scorer.log
                self._reply(400, b"{}")

        def log_message(self, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("AUTOSWITCH_PORT", "8765")))
    ap.add_argument("--models", default=os.environ.get("AUTOSWITCH_MODELS", os.path.join(os.path.dirname(HERE), "models")))
    args = ap.parse_args()
    if sys.stdout is None or sys.stderr is None:  # started with pythonw.exe on Windows: no console, so log to a file
        logdir = os.path.join(os.path.dirname(os.path.abspath(args.models)), "logs")
        os.makedirs(logdir, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(logdir, "scorer.log"), "a", buffering=1, encoding="utf-8")
    handler = make_handler(load(args.models, "synthetic"), load(args.models, "measured"))
    print(f"autoswitch scorer on http://127.0.0.1:{args.port}", flush=True)
    for fd in (1, 2):  # launchd (or the Windows scheduled task) appends to logs/scorer.log forever; start it fresh once it passes 5 MB
        try:
            st = os.fstat(fd)
            if stat.S_ISREG(st.st_mode) and st.st_size > 5 * 1024 * 1024:
                os.ftruncate(fd, 0)
        except OSError:
            pass
    HTTPServer(("127.0.0.1", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
