#!/usr/bin/env python3
"""skill_pending_review — backlog/Auto/digest CLI for the pending skills queue.

Modes:
  backlog  report per-skill merge (keep/superseded + LLM suggestion). With
           --dry-run (default) writes the report only; without, execute.
  auto     deterministic gate -> LLM judge -> apply/archive, bounded by
           --limit; keeps an exclusive lock.
  digest   build a one-message Telegram digest of the last review cycle.

Run with HERMES_HOME=<profile root> so pending lives under the right profile.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import skill_review_core as core  # noqa: E402

LOCK_STALE_S = 30 * 60


class _Lock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.acquired = False

    def try_acquire(self) -> bool:
        import fcntl
        self.fd = open(self.lock_path, "w")
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd.write(f"pid={os.getpid()} ts={time.time()}\n")
            self.fd.flush()
            self.acquired = True
            return True
        except OSError:
            # stale-lock takeover: if mtime old enough, steal it
            try:
                if time.time() - self.lock_path.stat().st_mtime > LOCK_STALE_S:
                    fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX)
                    self.fd.write(f"pid={os.getpid()} ts={time.time()} (stale takeover)\n")
                    self.fd.flush()
                    self.acquired = True
                    return True
            except Exception:
                pass
            self.fd.close()
            return False

    def release(self):
        if self.acquired:
            try:
                import fcntl
                fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self.fd.close()


def _hermes_home(args) -> Path:
    hh = getattr(args, "hermes_home", None) or os.environ.get("HERMES_HOME")
    return Path(hh) if hh else Path.home() / ".hermes"


def run_backlog(args) -> int:
    hh = _hermes_home(args)
    recs = core.load_pending(hh, limit=args.limit)
    groups = core.group_by_skill(recs)
    report_lines = [f"# skill pending backlog — {len(recs)} records / {len(groups)} skills"]
    api_key = core.openrouter_key(hh)
    total_keep = total_sup = total_manual = 0
    for name in sorted(groups):
        grp = groups[name]
        mg = core.merge_group(name, grp)
        keep, sup = mg["keep"], mg["superseded"]
        total_keep += len(keep)
        total_sup += len(sup)
        report_lines.append(f"\n## {name} — keep={len(keep)} superseded={len(sup)}")
        for k in keep:
            p = k["record"].get("payload") or {}
            cl = core.classify(p)
            verdict = "manual" if cl["level"] == "manual" else "llm->?"
            if cl["level"] == "llm" and getattr(args, "llm_suggest", False) and api_key:
                j = core.llm_judge(name, p, api_key)
                jd = j["decision"]
                verdict = "llm->approve" if jd == "approve" else (
                    "llm->reject" if jd == "reject" else f"llm->{jd}")
                if jd in ("error",):
                    total_manual += 1
                report_lines.append(f"  KEEP    [{verdict}] {p.get('action')} ({k['strategy']}) {p.get('file_path') or ''}")
                report_lines.append(f"          llm: {j.get('reason','')[:160]}")
            elif cl["level"] == "manual":
                total_manual += 1
                report_lines.append(f"  KEEP    [manual] {p.get('action')} ({k['strategy']}) {p.get('file_path') or ''}")
            else:
                verdict = "llm->(dry-run 不查, 需 --llm-suggest)"
                report_lines.append(f"  KEEP    [{verdict}] {p.get('action')} ({k['strategy']})")
        for s in sup:
            report_lines.append(f"  SUPERSEDED {s['record'].get('payload',{}).get('action')} — {s['reason'][:110]}")
    report_lines.append(f"\nTOTAL keep={total_keep} superseded={total_sup} manual={total_manual}")

    if args.outfile:
        out = Path(args.outfile)
        out.write_text("\n".join(report_lines), encoding="utf-8")
        print(f"report -> {out}")
    else:
        print("\n".join(report_lines))
    return 0


def run_auto(args) -> int:
    hh = _hermes_home(args)
    lock = _Lock(hh / "pending" / ".skill_review.lock")
    if not lock.try_acquire():
        print("lock held")
        return 0
    try:
        recs = core.load_pending(hh, limit=args.limit)
        groups = core.group_by_skill(recs)
        api_key = core.openrouter_key(hh)
        applied = archived = left = failed = 0
        for name in sorted(groups):
            grp = groups[name]
            mg = core.merge_group(name, grp)
            for k in mg["keep"]:
                p = k["record"].get("payload") or {}
                cl = core.classify(p)
                if cl["level"] == "manual":
                    core.execute_decision(k["record"], "leave", hh)
                    left += 1
                    continue
                if not api_key:
                    core.execute_decision(k["record"], "leave", hh)
                    left += 1
                    continue
                j = core.llm_judge(name, p, api_key)
                if j["decision"] == "approve":
                    r = core.execute_decision(k["record"], "apply", hh)
                    applied += 1 if r["status"] == "applied" else 0
                    failed += 0 if r["status"] == "applied" else 1
                elif j["decision"] == "reject":
                    core.execute_decision(k["record"], "archive", hh)
                    archived += 1
                else:
                    core.append_log(hh, {"ts": time.time(), "id": k["record"].get("id"),
                                          "skill": name,
                                          "decision": "llm_error",
                                          "detail": j.get("reason", "")[:200],
                                          "action": p.get("action")})
                    core.execute_decision(k["record"], "leave", hh)
                    left += 1
            for s in mg["superseded"]:
                core.execute_decision(s["record"], "archive", hh)
                archived += 1
        core.append_log(hh, {"ts": time.time(), "event": "auto_cycle",
                             "applied": applied, "archived": archived,
                             "left": left, "failed": failed})
        print(f"auto: applied={applied} archived={archived} left={left} failed={failed}")
        return 0
    finally:
        lock.release()


def run_digest(args) -> int:
    hh = _hermes_home(args)
    log = core.log_path(hh)
    if not log.exists():
        print("digest: no review log yet")
        return 0
    lines = log.read_text(encoding="utf-8").splitlines()[-200:]
    applied = archived = left = 0
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue
        d = e.get("decision")
        applied += d == "apply"
        archived += d == "archive"
        left += d == "leave"
    remaining = len(core.load_pending(hh))
    msg = (f"skill review digest: applied={applied} archived={archived} "
           f"left(manual)={left} | remaining pending={remaining}")
    if applied == archived == left == 0:
        msg = "skill review digest: heartbeat — no activity since last cycle."
    if args.send:
        _send_telegram(msg, args)
    print(msg)
    return 0


def _send_telegram(msg: str, args):
    tok = getattr(args, "tg_token", None) or os.environ.get("MANNIE_TG_BOT_TOKEN")
    chat = getattr(args, "tg_chat", None) or os.environ.get("MANNIE_TG_CHAT_ID")
    if not (tok and chat):
        print("digest: tg_token/tg_chat missing, printed only")
        return
    import urllib.request
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    data = json.dumps({"chat_id": chat, "text": msg}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print(f"digest: telegram send failed: {e}")


def main():
    p = argparse.ArgumentParser(description="skill pending review")
    p.add_argument("--mode", choices=["backlog", "auto", "digest"], required=True)
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="backlog: report only (default True)")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    p.add_argument("--llm-suggest", action="store_true",
                   help="backlog: also call LLM judge per clean keep (slow, network)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--outfile", default=None)
    p.add_argument("--hermes-home", default=None)
    p.add_argument("--send", action="store_true", help="digest: send to Telegram")
    p.add_argument("--tg-token", default=None)
    p.add_argument("--tg-chat", default=None)
    args = p.parse_args()

    if args.mode == "backlog":
        return run_backlog(args)
    if args.mode == "auto":
        return run_auto(args)
    if args.mode == "digest":
        return run_digest(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())