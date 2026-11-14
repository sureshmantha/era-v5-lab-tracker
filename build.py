"""Build the lab progress page from the repo, git history and GitHub pull requests.

    python3 build.py --repo ../era-v5-lab    # fetch the lab repo, read GitHub, write out/index.html
    python3 build.py --offline               # no network: use the last fetch and the cached API answers
    GITHUB_TOKEN=... python3 build.py --reviews   # also read review states and the last comment

Sources, in order of trust:
  1. The lab's main branch (what is merged) and refs/pull/*/head (what each PR changes), read with git.
     They are fetched by URL into refs/remotes/tracker/*, so your own remotes and branches are not touched.
  2. The GitHub REST API for PR and issue titles, states, dates and comment counts (cached in cache/).
  3. plan.yaml (what the lab plans) and learn.yaml (notes for learning). Both are edited by hand.
The token is read from the environment only. It is never written to a file.
"""

import argparse
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUT = HERE / "out"
UTC = dt.timezone.utc

TEXT_EXT = {".md", ".py", ".yaml", ".yml", ".txt", ".toml", ".sh", ".cfg", ".ini", ".html"}
CODE_EXT = {".py", ".sh"}
MAX_BLOB = 300_000
TEAM_PATH_RE = re.compile(r"^teams/(\d{2})-[^/]+/")
REF_RES = [
    re.compile(r"teams/(\d{2})-"),
    re.compile(r"\b(?:G|Team\s?|team\s)0?(\d{1,2})\b"),
]
TODO_RE = re.compile(r"(?:#|//|<!--)\s*(TODO|FIXME|XXX)\b[:\s]*(.{0,160})")
OPEN_HEAD_RE = re.compile(r"^#{2,4}\s+(?:\d+[.)]?\s*)?(open (?:items|questions|issues)|todo|next steps?|risks?|blockers?|known (?:gaps|issues)|what (?:is|v\d) not)", re.I)
TASK_RE = re.compile(r"week[\s-]*(\d+)\D{0,12}?task\s*(\d+)", re.I)
THREE_DAY_H = 72


# ---------------------------------------------------------------- git

def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def git_fetch(repo, remote):
    url = f"https://github.com/{repo}.git"
    print(f"fetching {url} main and PR heads ...", file=sys.stderr)
    r = subprocess.run(["git", "fetch", "-q", "--no-tags", url, f"+refs/heads/main:refs/remotes/{remote}/main",
                        f"+refs/pull/*/head:refs/remotes/{remote}/pr/*"], cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        print(f"warning: git fetch failed, using the last fetch: {r.stderr.strip()}", file=sys.stderr)


def find_lab_repo(given):
    """--repo, then $ERA_LAB_REPO, then the parent folder, then a sibling era-v5-lab folder."""
    candidates = [given, os.environ.get("ERA_LAB_REPO"), HERE.parent, HERE.parent / "era-v5-lab"]
    for c in candidates:
        if c and (Path(c) / ".git").exists() and (Path(c) / "teams").is_dir():
            return Path(c).resolve()
    raise SystemExit("cannot find the era-v5-lab checkout: pass --repo /path/to/era-v5-lab "
                     "or set ERA_LAB_REPO (see README.md)")


def main_files(ref):
    out = git("ls-tree", "-r", "-l", ref)
    files = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        _, kind, sha, size = meta.split()
        if kind == "blob":
            files[path] = (sha, int(size) if size != "-" else 0)
    return files


def read_blobs(shas):
    """Read many blobs in one git process. Returns {sha: text}."""
    if not shas:
        return {}
    p = subprocess.Popen(["git", "cat-file", "--batch"], cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    data, _ = p.communicate("\n".join(shas).encode() + b"\n")
    out, i = {}, 0
    for sha in shas:
        nl = data.index(b"\n", i)
        header = data[i:nl].split()
        size = int(header[2])
        out[sha] = data[nl + 1:nl + 1 + size].decode("utf-8", "replace")
        i = nl + 1 + size + 1
    return out


def has_object(sha):
    return subprocess.run(["git", "cat-file", "-e", sha], cwd=REPO, capture_output=True).returncode == 0


def pr_changes(remote, number, base_sha):
    head = f"refs/remotes/{remote}/pr/{number}"
    if not has_object(head):
        return None
    base = base_sha if base_sha and has_object(base_sha) else f"{remote}/main"
    out = git("diff", "--numstat", "--no-renames", f"{base}...{head}", check=False)
    files, add, dele = [], 0, 0
    for line in out.splitlines():
        a, d, path = line.split("\t", 2)
        files.append(path)
        add += int(a) if a.isdigit() else 0
        dele += int(d) if d.isdigit() else 0
    return {"files": files, "add": add, "del": dele}


def commit_log(ref, since):
    out = git("log", ref, f"--since={since}", "--no-merges", "--format=@%H|%aI|%s", "--name-only", "--", "teams")
    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("@"):
            sha, when, subject = line[1:].split("|", 2)
            cur = {"sha": sha, "at": when, "subject": subject, "teams": set()}
            commits.append(cur)
        elif line and cur is not None:
            m = TEAM_PATH_RE.match(line)
            if m:
                cur["teams"].add(m.group(1))
    return commits


# ---------------------------------------------------------------- GitHub API

def api_get_all(path, token, offline):
    """GET every page of a list endpoint. Falls back to the cache when offline or refused."""
    cache = CACHE / (re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") + ".json")
    if offline:
        if cache.exists():
            return json.loads(cache.read_text())
        raise SystemExit(f"--offline but no cache for {path}; run once with network")
    url = f"https://api.github.com/{path}"
    url += ("&" if "?" in url else "?") + "per_page=100"
    items = []
    try:
        while url:
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": "era-v5-tracker"})
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=30) as r:
                items.extend(json.load(r))
                link = r.headers.get("Link", "")
                m = re.search(r'<([^>]+)>;\s*rel="next"', link)
                url = m.group(1) if m else None
    except (urllib.error.URLError, TimeoutError) as e:
        if cache.exists():
            print(f"warning: {path}: {e}; using the cache", file=sys.stderr)
            return json.loads(cache.read_text())
        raise SystemExit(f"GitHub API {path}: {e}")
    CACHE.mkdir(exist_ok=True)
    cache.write_text(json.dumps(items))
    return items


def review_info(repo, pr, token):
    """Latest review state and last comment for one PR, cached by its updated_at."""
    cache = CACHE / "reviews.json"
    store = json.loads(cache.read_text()) if cache.exists() else {}
    key = str(pr["number"])
    if store.get(key, {}).get("updated") == pr["updated_at"]:
        return store[key]
    info = {"updated": pr["updated_at"], "review": None, "last_comment": None}
    try:
        reviews = api_get_all(f"repos/{repo}/pulls/{key}/reviews", token, False)
        states = [r["state"] for r in reviews if r["state"] in ("APPROVED", "CHANGES_REQUESTED")]
        info["review"] = states[-1] if states else ("COMMENTED" if reviews else None)
        comments = api_get_all(f"repos/{repo}/issues/{key}/comments", token, False)
        if comments:
            c = comments[-1]
            info["last_comment"] = {"by": c["user"]["login"], "at": c["created_at"],
                                    "text": re.sub(r"\s+", " ", c["body"] or "")[:240]}
    except SystemExit as e:
        print(f"warning: reviews of #{key}: {e}", file=sys.stderr)
        return info
    store[key] = info
    cache.write_text(json.dumps(store))
    return info


# ---------------------------------------------------------------- helpers

def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def week_of(t, start):
    if t is None:
        return None
    return max(0, (t.date() - start).days // 7 + 1)


def team_of_path(path):
    m = TEAM_PATH_RE.match(path)
    if m:
        return m.group(1)
    if path.startswith("diagnosis/"):
        return "diag"
    return "lab"


def first_heading(text):
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return None


def open_items(text):
    """Bullets under headings such as 'Open items', 'Risks', 'Next steps'."""
    found, cur, level = [], None, 0
    for line in text.splitlines():
        if line.startswith("#"):
            lvl = len(line) - len(line.lstrip("#"))
            if cur is not None and lvl <= level:
                cur = None
            if OPEN_HEAD_RE.match(line):
                cur = {"heading": line.lstrip("#").strip(), "items": []}
                level = lvl
                found.append(cur)
            continue
        if cur is not None and re.match(r"^\s*(?:[-*]|\d+\.|\|)\s+", line) and not re.match(r"^\s*\|[-:| ]+\|\s*$", line):
            item = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line).strip()
            if item.startswith("|"):
                item = " · ".join(c.strip() for c in item.strip("|").split("|") if c.strip())
            if item and len(cur["items"]) < 12:
                cur["items"].append(item[:300])
    return [f for f in found if f["items"]]


# ---------------------------------------------------------------- build

def build(args):
    plan = yaml.safe_load((HERE / "plan.yaml").read_text())
    learn = yaml.safe_load((HERE / "learn.yaml").read_text()) if (HERE / "learn.yaml").exists() else {}
    start = plan["lab"]["start"]
    repo = plan["lab"]["repo"]
    remote = args.remote
    ref = f"{remote}/main"
    now = dt.datetime.now(UTC)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    if not args.offline and not args.no_fetch:
        git_fetch(repo, remote)
    if subprocess.run(["git", "rev-parse", "-q", "--verify", ref], cwd=REPO, capture_output=True).returncode:
        raise SystemExit(f"{ref} does not exist yet: run once without --offline / --no-fetch")

    # ---- what is merged
    files = main_files(ref)
    main_sha = git("rev-parse", ref).strip()
    team_dirs = sorted({p.split("/")[1] for p in files if p.startswith("teams/") and p.count("/") >= 2})
    teams = {}
    for slug in team_dirs:
        tid = slug[:2]
        meta = plan.get("teams", {}).get(tid, {})
        teams[tid] = {"id": tid, "slug": slug, "short": f"G{int(tid)}", "name": meta.get("name", slug[3:]),
                      "mine": tid == args.my_team or (not args.my_team and bool(meta.get("mine"))), "files": 0, "bytes": 0, "loc": 0, "code_files": 0,
                      "tests": 0, "docs": [], "readme": "", "refs_out": Counter(), "todos": [],
                      "open_items": [], "jobs": [], "prs": [], "issues": [], "tasks": [],
                      "commits_week": Counter(), "last_commit": None, "ext": Counter()}

    text_paths = [p for p, (sha, size) in files.items()
                  if p.startswith("teams/") and Path(p).suffix in TEXT_EXT and size <= MAX_BLOB]
    blobs = read_blobs([files[p][0] for p in text_paths])
    for p, (sha, size) in files.items():
        tid = team_of_path(p)
        if tid not in teams:
            continue
        t = teams[tid]
        rel = p.split("/", 2)[2]
        t["files"] += 1
        t["bytes"] += size
        t["ext"][Path(p).suffix or "(none)"] += 1
        name = Path(p).name
        if Path(p).suffix in CODE_EXT:
            t["code_files"] += 1
        if re.match(r"(test_.*|.*_test)\.py$", name):
            t["tests"] += 1
        if rel.startswith("jobs/") and p.endswith(".md") and name.lower() != "readme.md":
            t["jobs"].append(p)
        text = blobs.get(sha)
        if text is None:
            continue
        if Path(p).suffix in CODE_EXT:
            t["loc"] += text.count("\n")
            for i, line in enumerate(text.splitlines(), 1):
                m = TODO_RE.search(line)
                if m and len(t["todos"]) < 40:
                    t["todos"].append({"path": p, "line": i, "text": f"{m.group(1)}: {m.group(2).strip()}"})
        if p.endswith(".md"):
            if rel == "README.md":
                t["readme"] = text
            t["docs"].append({"path": p, "title": first_heading(text) or name, "bytes": size})
            for oi in open_items(text):
                t["open_items"].append({"path": p, **oi})
        if "/support/simulation/" in p:
            continue
        for rx in REF_RES:
            for m in rx.finditer(text):
                other = m.group(1).zfill(2)
                if other != tid and other in teams:
                    t["refs_out"][other] += 1

    # ---- git activity on main
    for c in commit_log(ref, (start - dt.timedelta(days=14)).isoformat()):
        when = parse_time(c["at"])
        w = week_of(when, start)
        for tid in c["teams"]:
            if tid in teams:
                teams[tid]["commits_week"][w] += 1
                if not teams[tid]["last_commit"] or c["at"] > teams[tid]["last_commit"]["at"]:
                    teams[tid]["last_commit"] = {"at": c["at"], "sha": c["sha"][:7], "subject": c["subject"][:120]}

    # ---- pull requests and issues
    raw_prs = api_get_all(f"repos/{repo}/pulls?state=all", token, args.offline)
    raw_issues = api_get_all(f"repos/{repo}/issues?state=all", token, args.offline)
    comment_counts = {i["number"]: i["comments"] for i in raw_issues}
    prs = []
    for p in raw_prs:
        n = p["number"]
        ch = pr_changes(remote, n, p["base"]["sha"]) or {"files": [], "add": 0, "del": 0}
        tids = sorted({team_of_path(f) for f in ch["files"]}) or ["lab"]
        created, merged, closed = parse_time(p["created_at"]), parse_time(p["merged_at"]), parse_time(p["closed_at"])
        state = "merged" if merged else ("closed" if p["state"] == "closed" else "open")
        end = merged or closed or now
        tm = TASK_RE.search(p["title"])
        pr = {"n": n, "title": p["title"], "url": p["html_url"], "author": p["user"]["login"], "state": state,
              "draft": p.get("draft", False), "created": p["created_at"], "merged": p["merged_at"],
              "closed": p["closed_at"], "updated": p["updated_at"], "head": p["head"]["ref"],
              "comments": comment_counts.get(n, 0), "labels": [l["name"] for l in p["labels"]],
              "teams": tids, "files": ch["files"][:60], "nfiles": len(ch["files"]), "add": ch["add"],
              "del": ch["del"], "week": week_of(created, start),
              "age_h": round((end - created).total_seconds() / 3600, 1),
              "task": {"week": int(tm.group(1)), "n": int(tm.group(2))} if tm else None,
              "body": re.sub(r"\s+", " ", p.get("body") or "")[:400]}
        if args.reviews and token and state == "open":
            info = review_info(repo, p, token)
            pr["review"], pr["last_comment"] = info["review"], info["last_comment"]
        prs.append(pr)
        for tid in tids:
            if tid in teams:
                teams[tid]["prs"].append(n)
    issues = []
    for i in raw_issues:
        if "pull_request" in i:
            continue
        m = re.search(r"\[G(\d{1,2})\]|\bG(\d{1,2})\b|Team\s?0?(\d{1,2})\b", i["title"])
        tid = (m.group(1) or m.group(2) or m.group(3)).zfill(2) if m else None
        issue = {"n": i["number"], "title": i["title"], "url": i["html_url"], "state": i["state"],
                 "author": i["user"]["login"], "created": i["created_at"], "closed": i["closed_at"],
                 "comments": i["comments"], "team": tid}
        issues.append(issue)
        if tid in teams:
            teams[tid]["issues"].append(i["number"])

    by_n = {p["n"]: p for p in prs}

    # ---- planned tasks
    today = now.date()
    tasks = []
    for k, task in enumerate(plan.get("tasks") or []):
        path = task["path"]
        on_main = path in files or any(f.startswith(path.rstrip("/") + "/") for f in files)
        if on_main and task.get("check"):
            text = read_blobs([files[path][0]]).get(files[path][0], "") if path in files else ""
            on_main = task["check"] in text
        touching = [p for p in prs if any(f == path or f.startswith(path.rstrip("/") + "/") for f in p["files"])]
        open_prs = [p["n"] for p in touching if p["state"] == "open"]
        merged_prs = [p["n"] for p in touching if p["state"] == "merged"]
        due = task.get("due")
        status = ("done" if on_main else "review" if open_prs else
                  "late" if due and due < today else "todo")
        tasks.append({"id": k, "team": task["team"], "week": task.get("week"), "owner": task.get("owner"),
                      "title": task["title"], "path": path, "due": str(due) if due else None, "status": status,
                      "open_prs": open_prs, "merged_prs": merged_prs, "needs": task.get("needs"),
                      "source": "plan"})
    # Tasks the PR titles name ("week 2 task 6") that the plan does not list yet.
    for p in prs:
        if p["task"] and p["teams"][0] in teams:
            tasks.append({"id": f"pr{p['n']}", "team": p["teams"][0], "week": p["task"]["week"], "owner": None,
                          "title": f"Task {p['task']['n']}: {p['title']}", "path": None, "due": None,
                          "status": {"merged": "done", "open": "review", "closed": "todo"}[p["state"]],
                          "open_prs": [p["n"]] if p["state"] == "open" else [],
                          "merged_prs": [p["n"]] if p["state"] == "merged" else [], "source": "pr title"})
    for t in tasks:
        if t["team"] in teams:
            teams[t["team"]]["tasks"].append(t["id"])

    # ---- signals per team
    this_week = week_of(now, start)
    for tid, t in teams.items():
        mine = [by_n[n] for n in t["prs"]]
        open_ = [p for p in mine if p["state"] == "open"]
        merged = [p for p in mine if p["state"] == "merged"]
        merged7 = [p for p in merged if now - parse_time(p["merged"]) <= dt.timedelta(days=7)]
        stale = [p for p in open_ if p["age_h"] > THREE_DAY_H]
        quiet = [p for p in open_ if p["comments"] == 0 and p["age_h"] > 24]
        lat = [p["age_h"] for p in merged]
        ttasks = [x for x in tasks if x["team"] == tid]
        late = [x for x in ttasks if x["status"] == "late"]
        last = parse_time(t["last_commit"]["at"]) if t["last_commit"] else None
        idle_days = (now - last).days if last else None
        reasons, good = [], []
        if late:
            reasons.append(f"{len(late)} planned task(s) past due")
        if stale:
            reasons.append(f"{len(stale)} PR(s) open more than 3 days (the 3-day rule)")
        if quiet:
            reasons.append(f"{len(quiet)} PR(s) with no comment after a day: need a review")
        if idle_days is not None and idle_days >= 4:
            reasons.append(f"no merge into the team folder for {idle_days} days")
        if merged7:
            good.append(f"{len(merged7)} PR(s) merged in the last 7 days")
        if lat:
            good.append(f"median time to merge {statistics.median(lat):.0f} h")
        if t["tests"]:
            good.append(f"{t['tests']} test file(s)")
        if t["jobs"]:
            good.append(f"{len(t['jobs'])} TSAI job request(s)")
        red = bool(late) or (len(stale) >= 3) or (not merged7 and len(open_) >= 2)
        rag = "red" if red else ("amber" if reasons else "green")
        if t["code_files"] and not t["tests"]:
            reasons.append("no test files (test_*.py) yet; not counted in the status")
        t["signals"] = {"rag": rag, "reasons": reasons, "good": good, "open": len(open_), "merged": len(merged),
                        "merged7": len(merged7), "stale": [p["n"] for p in stale], "quiet": [p["n"] for p in quiet],
                        "closed_unmerged": sum(p["state"] == "closed" for p in mine),
                        "median_merge_h": round(statistics.median(lat), 1) if lat else None,
                        "idle_days": idle_days,
                        "tasks_done": sum(x["status"] == "done" for x in ttasks), "tasks": len(ttasks)}
        prw = Counter(p["week"] for p in mine)
        mw = Counter(week_of(parse_time(p["merged"]), start) for p in merged)
        t["weekly"] = {str(w): {"opened": prw.get(w, 0), "merged": mw.get(w, 0), "commits": t["commits_week"].get(w, 0)}
                       for w in range(0, max(this_week, 1) + 1)}
        t["refs_out"] = dict(t["refs_out"].most_common())
        t["ext"] = dict(t["ext"].most_common(6))
        t["commits_week"] = {str(k): v for k, v in t["commits_week"].items()}
        t["learn"] = (learn.get("teams") or {}).get(tid, {})

    data = {
        "generated": now.isoformat(timespec="seconds"), "main_sha": main_sha[:7], "repo": repo,
        "this_week": this_week, "lab": {**plan["lab"], "start": str(start)},
        "weeks": {str(k): v for k, v in (plan.get("weeks") or {}).items()},
        "stages": plan.get("stages") or [], "contracts": plan.get("contracts") or [],
        "teams": teams, "prs": prs, "issues": issues, "tasks": tasks,
        "learn": {k: v for k, v in learn.items() if k != "teams"},
        "token_used": bool(token), "reviews": bool(args.reviews and token),
        "diag": {"open": sum(p["state"] == "open" and p["teams"] == ["diag"] for p in prs),
                 "merged": sum(p["state"] == "merged" and p["teams"] == ["diag"] for p in prs)},
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "data.json").write_text(json.dumps(data, indent=1, default=list))
    page = (HERE / "template.html").read_text()
    blob = json.dumps(data, default=list).replace("</", "<\\/")
    (OUT / "index.html").write_text(page.replace("/*__DATA__*/null", blob))
    print(f"wrote {OUT / 'index.html'}: {len(teams)} teams, {len(prs)} PRs, {len(issues)} issues, "
          f"{len(tasks)} tasks, main {main_sha[:7]}, week {this_week}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="your era-v5-lab checkout (default: $ERA_LAB_REPO, the parent or a sibling folder)")
    ap.add_argument("--remote", default="tracker", help="ref namespace for the fetched lab refs (default: tracker)")
    ap.add_argument("--my-team", help="two-digit team number to highlight, for example 07")
    ap.add_argument("--offline", action="store_true", help="no network: cached API answers and the last fetch")
    ap.add_argument("--no-fetch", action="store_true", help="skip git fetch but still read the API")
    ap.add_argument("--reviews", action="store_true", help="read review states and last comments (needs GITHUB_TOKEN)")
    args = ap.parse_args()
    if args.my_team:
        args.my_team = args.my_team.zfill(2)
    REPO = find_lab_repo(args.repo)
    build(args)
