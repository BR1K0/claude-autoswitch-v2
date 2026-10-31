"""Read the real git state of a repository. No model involved."""
import subprocess


def _git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                       text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else None


def state(repo):
    if _git(repo, "rev-parse", "--git-dir") is None:
        return {"repo": repo, "error": "not a git repository"}
    status = _git(repo, "status", "--porcelain") or ""
    lines = [l for l in status.splitlines() if l]
    return {
        "repo": repo,
        "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "head": _git(repo, "rev-parse", "--short", "HEAD"),
        "staged": [l[3:] for l in lines if l[0] not in " ?"],
        "modified": [l[3:] for l in lines if l[1] == "M"],
        "untracked": [l[3:] for l in lines if l.startswith("??")],
        "diffstat": _git(repo, "diff", "--stat", "HEAD") or "",
        "recent_commits": (_git(repo, "log", "--oneline", "-8") or "").splitlines(),
    }


def record(store, repo):
    """Snapshot git state into the journal as an observed event."""
    s = state(repo)
    summary = "Git %s @ %s: %d staged, %d modified, %d untracked" % (
        s.get("branch"), s.get("head"), len(s.get("staged", [])),
        len(s.get("modified", [])), len(s.get("untracked", [])))
    key = "git:%s:%s:%s" % (s.get("head"), len(s.get("modified", [])),
                            hash(s.get("diffstat")))
    import json
    return store.append(key, "git_state", "observed", summary, json.dumps(s, indent=1))
