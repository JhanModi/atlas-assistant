"""atlas: command-line entry point.

  atlas doctor                  check Python, config, Ollama, models, GPU
  atlas seed-demo | clear-demo  load/remove fictional demo data (until real connectors exist)
  atlas ask "what's due?"       one question through the full pipeline
  atlas serve                   local dashboard on http://127.0.0.1:8765
  atlas bench [--models ...]    benchmark local models (VRAM, speed, tool-call validity, latency)
  atlas kpis [--days 7]         print the KPI baseline
  atlas auth canvas|gcal|status store credentials you enter (OS keyring); show source health
  atlas sync [--source S] [--dry-run]   sync routine / Canvas / Google Calendar into the store
  atlas brief [--if-due] [--verdict accurate|inaccurate]   daily briefing; record the Phase 2 gate check
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys


def _ok(msg: str) -> None:
    print(f"  [ok]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def _fail(msg: str) -> None:
    print(f"  [fail] {msg}")


def cmd_doctor(_: argparse.Namespace) -> int:
    from importlib.metadata import version

    from atlas.config import get_settings, load_policy, project_root
    from atlas.web.app import ollama_status

    s = get_settings()
    problems = 0
    print("Atlas doctor\n")
    print(f"  project root: {project_root()}")
    py = sys.version_info
    (_ok if py >= (3, 11) else _fail)(f"Python {platform.python_version()}")
    _ok(f"strands-agents {version('strands-agents')}")

    root = project_root()
    if (root / "config" / "settings.toml").exists():
        _ok("config/settings.toml found")
    else:
        _warn("config/settings.toml not found: using defaults (copy config/settings.example.toml to customise)")
    try:
        pol = load_policy()
        _ok(f"policy.yaml: {len(pol.tools)} tools, contexts {sorted(pol.contexts)}")
    except Exception as exc:  # noqa: BLE001
        _fail(f"policy.yaml unreadable: {exc}")
        problems += 1
    try:
        s.data_path.mkdir(parents=True, exist_ok=True)
        _ok(f"data dir {s.data_path}")
    except OSError as exc:
        _fail(f"data dir not writable: {exc}")
        problems += 1

    st = ollama_status(s)
    if not st["reachable"]:
        _fail(f"Ollama not reachable at {s.model.host}")
        print("         Install it from https://ollama.com/download (free), start it, then re-run `atlas doctor`.")
        problems += 1
    else:
        _ok(f"Ollama reachable; models pulled: {', '.join(st['models']) or 'none'}")
        if st["model_ready"]:
            _ok(f"default model {s.model.model_id} is pulled")
        else:
            _fail(f"default model {s.model.model_id} not pulled. Run: ollama pull {s.model.model_id}")
            problems += 1

    if shutil.which("nvidia-smi"):
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if out:
            name, total, used = [x.strip() for x in out.splitlines()[0].split(",")]
            _ok(f"GPU {name}: {used}/{total} MiB VRAM in use")
    else:
        _warn("nvidia-smi not found: GPU checks skipped (models will run on CPU if no GPU is usable)")

    print("\nAll good." if problems == 0 else f"\n{problems} problem(s) to fix.")
    return 0 if problems == 0 else 1


def cmd_seed_demo(_: argparse.Namespace) -> int:
    from atlas.evaluation.demo_data import seed_demo
    from atlas.store import get_engine

    n = seed_demo(get_engine())
    print(f"Loaded {n} fictional demo items (source=demo). Remove them with `atlas clear-demo`.")
    return 0


def cmd_clear_demo(_: argparse.Namespace) -> int:
    from atlas.evaluation.demo_data import clear_demo
    from atlas.store import get_engine

    clear_demo(get_engine())
    print("Demo items removed.")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from atlas.agent import Assistant
    from atlas.store import get_engine

    assistant = Assistant(get_engine())
    result = assistant.handle(" ".join(args.text), context="chat")
    print(result.reply)
    if result.approvals:
        print("\nNeeds approval (use the dashboard):", json.dumps(result.approvals, indent=2, default=str))
    m = result.metrics
    if m:
        print(f"\n[{result.status}] total {m['e2e_ms']:.0f} ms · model {m['model_ms']:.0f} ms · "
              f"tools {m['tool_ms']:.0f} ms · first token {m['ttfb_ms'] or 0:.0f} ms · tools used: {result.tools or '-'}")
    return 0 if result.status != "failed" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from atlas.config import get_settings
    from atlas.proactive.notify import ToastNotifier
    from atlas.proactive.scheduler import AtlasScheduler
    from atlas.store import get_engine
    from atlas.web.app import create_app

    s = get_settings()
    host = s.web.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"Refusing to bind to {host}: the dashboard has no login. Use 127.0.0.1.")
        return 2
    url = f"http://{host}:{args.port or s.web.port}"
    scheduler = None if args.no_scheduler else AtlasScheduler(get_engine(), s, ToastNotifier(), url)
    print(f"Atlas dashboard: {url}")
    if scheduler:
        print(f"Background jobs: sync every {s.sync.interval_minutes} min; daily briefing at "
              f"{s.briefing.hour:02d}:{s.briefing.minute:02d} (catches up after sleep).")
    uvicorn.run(create_app(scheduler=scheduler), host=host, port=args.port or s.web.port, log_level="warning")
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    from atlas import timeutil
    from atlas.config import get_settings
    from atlas.proactive import briefing
    from atlas.proactive.notify import NullNotifier, ToastNotifier
    from atlas.proactive.scheduler import AtlasScheduler
    from atlas.store import get_engine

    s, engine, now = get_settings(), get_engine(), timeutil.now()
    if args.verdict:
        target = briefing.latest(engine, timeutil.to_local(now).date().isoformat())
        if target is None:
            print("No briefing for today yet. Run `atlas brief` first.")
            return 1
        briefing.set_verdict(engine, target.id, args.verdict, args.missed, args.note, now)
        print(f"Recorded: today's briefing was {args.verdict}"
              + (f", {args.missed} item(s) missed or wrong" if args.missed else "") + ".")
        return 0

    url = f"http://{s.web.host}:{s.web.port}"
    notifier = ToastNotifier() if args.notify else NullNotifier()
    runner = AtlasScheduler(engine, s, notifier, url)
    if args.if_due:  # for Windows Task Scheduler: idempotent, makes at most one briefing per day
        made = briefing.ensure_daily(engine, s, now, notifier, url,
                                     sync=None if args.no_sync else (lambda t: runner.sync_all(t, skip_fresh=True)))
        if made is None:
            print("Not due yet, or today's briefing already exists.")
            return 0
        print(briefing.render_text(made.content))
        return 0
    if not args.no_sync:
        for report in runner.sync_all(now, skip_fresh=True):
            print(report.line())
        print()
    made = briefing.generate(engine, s, now, trigger="manual", daily=False, notifier=None)
    print(briefing.render_text(made.content))
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from atlas.evaluation.bench import run_bench

    return run_bench(models=args.models, contexts=args.ctx, quick=args.quick, now=args.now)


def _reset_sync_state(source: str) -> None:
    """After a successful sign-in, clear needs_reauth/backoff so the next sync runs."""
    from atlas.store import SyncState, get_engine, session_scope

    with session_scope(get_engine()) as db:
        state = db.get(SyncState, source)
        if state:
            state.status, state.consecutive_failures, state.next_attempt_at = "never", 0, None
            db.add(state)
            db.commit()


def _check_feed(source: str, url: str, flavor: str) -> int:
    import icalendar

    from atlas.config import get_settings
    from atlas.connectors.base import SourceError
    from atlas.connectors.ics_feed import IcsFeedConnector

    try:
        raw = IcsFeedConnector(source, url, get_settings().sync, flavor, "atlas auth").download()
        events = icalendar.Calendar.from_ical(raw).walk("VEVENT")
    except (SourceError, ValueError) as exc:
        print(f"That feed didn't work: {exc}. Nothing was saved.")
        return 1
    except Exception as exc:  # noqa: BLE001 - network errors; never echo the URL
        print(f"Couldn't download the feed ({type(exc).__name__}). Nothing was saved.")
        return 1
    print(f"Feed OK: {len(events)} events.")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    import getpass

    from atlas.config import get_settings
    from atlas.vault import KNOWN, get_store

    store, s = get_store(), get_settings()
    if args.target == "status":
        return cmd_auth_status(store, s)
    if args.target == "forget":
        for name in KNOWN.get(args.source or "", []):
            store.delete(name)
        print(f"Removed stored credentials for {args.source}." if args.source in KNOWN else
              f"Use: atlas auth forget {'|'.join(KNOWN)}")
        return 0 if args.source in KNOWN else 2

    if args.target == "canvas" and args.ics:
        print("Canvas -> Calendar -> 'Calendar Feed' (bottom right) -> copy the link.")
        url = getpass.getpass("Paste the feed URL (input hidden): ").strip()
        if _check_feed("canvas", url, "canvas"):
            return 1
        store.set("canvas.ics_url", url)
        print('Saved to Windows Credential Manager. Set [sources.canvas] enabled = true, mode = "ics".')
    elif args.target == "canvas":
        import httpx

        from atlas.connectors.base import NotConfigured, make_client
        from atlas.connectors.canvas import normalize_base_url

        try:
            base = normalize_base_url(s.sources.canvas.base_url)
        except NotConfigured as exc:
            print(exc)
            return 2
        print(f"Canvas ({base}) -> Account -> Settings -> '+ New Access Token'. Give it a purpose and expiry date.")
        token = getpass.getpass("Paste the token (input hidden): ").strip()
        try:
            with make_client(base_url=base, headers={"Authorization": f"Bearer {token}"}) as c:
                r = c.get("/api/v1/users/self")
        except httpx.HTTPError as exc:
            print(f"Couldn't reach Canvas ({type(exc).__name__}). Nothing was saved.")
            return 1
        if r.status_code != 200:
            print(f"Canvas did not accept that token (HTTP {r.status_code}). Nothing was saved.")
            return 1
        store.set("canvas.token", token)
        print("Token accepted and saved to Windows Credential Manager. Set [sources.canvas] enabled = true.")
    elif args.target == "gcal" and args.ics:
        print("Google Calendar -> Settings -> your calendar -> 'Secret address in iCal format' -> copy.")
        url = getpass.getpass("Paste the secret address (input hidden): ").strip()
        if _check_feed("gcal", url, "generic"):
            return 1
        store.set("gcal.ics_url", url)
        print('Saved to Windows Credential Manager. Set [sources.gcal] enabled = true, mode = "ics".')
    elif args.target == "gcal":
        import webbrowser
        from pathlib import Path

        from atlas.connectors.base import AuthError
        from atlas.connectors.gcal import run_consent_flow

        if not args.client_file:
            print("Pass the OAuth client file you downloaded: atlas auth gcal --client-file client_secret_XXXX.json")
            return 2
        try:
            info = json.loads(Path(args.client_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Can't read {args.client_file}: {exc}")
            return 2
        if "installed" not in info:
            print('That is not a "Desktop app" OAuth client. Create one of type Desktop app and download its JSON.')
            return 2
        cid, csecret = info["installed"].get("client_id"), info["installed"].get("client_secret")

        def _open(url: str) -> None:
            print("Opening your browser to sign in to Google (read-only calendar access).")
            print(f"If it doesn't open, visit this address yourself:\n{url}\n")
            webbrowser.open(url)

        try:
            refresh = run_consent_flow(cid, csecret, open_browser=_open)
        except AuthError as exc:
            print(exc)
            return 1
        store.set("gcal.client_id", cid)
        store.set("gcal.client_secret", csecret)
        store.set("gcal.refresh_token", refresh)
        print("Signed in. Credentials saved to Windows Credential Manager. Set [sources.gcal] enabled = true.")
        print(f"You can now delete {args.client_file} (it is also gitignored).")
    else:
        print("Use: atlas auth canvas [--ics] | gcal [--client-file F | --ics] | status | forget SOURCE")
        return 2
    _reset_sync_state(args.target)
    return 0


def cmd_auth_status(store, s) -> int:
    from sqlmodel import select

    from atlas import timeutil
    from atlas.store import SyncState, get_engine, session_scope
    from atlas.vault import KNOWN

    with session_scope(get_engine()) as db:
        states = {st.source: st for st in db.exec(select(SyncState)).all()}
    print("Sources (credentials are shown only as present/absent)\n")
    for name, cfg in (("routine", s.sources.routine), ("canvas", s.sources.canvas), ("gcal", s.sources.gcal)):
        mode = getattr(cfg, "mode", "file")
        stored = [k.split(".", 1)[1] for k in KNOWN.get(name, []) if store.get(k)]
        st = states.get(name)
        health = (f"{st.status}, last success {timeutil.fmt_local(st.last_success_at) or 'never'}"
                  + (f", error: {st.last_error}" if st.last_error else "")) if st else "never synced"
        print(f"  {name:8} {'enabled ' if cfg.enabled else 'disabled'} mode={mode:5} "
              f"stored={','.join(stored) or '-':30} {health}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from atlas import timeutil
    from atlas.config import get_settings
    from atlas.connectors import build_connectors, sync_source
    from atlas.store import get_engine

    s = get_settings()
    connectors = build_connectors(s, only=args.source)
    if not connectors:
        print("No sources enabled. Enable them under [sources.*] in config/settings.toml.")
        return 2
    worst = 0
    for conn in connectors:
        report = sync_source(get_engine(), conn, s.sync, force=args.force, dry_run=args.dry_run)
        print(report.line())
        worst = max(worst, 0 if report.status == "ok" or report.skipped else 1)
        if args.dry_run and report.items:
            for it in sorted(report.items, key=lambda i: i.start or i.due)[:80]:
                when = timeutil.fmt_local(it.due or it.start)
                flags = " ".join(f for f in (it.status, "untrusted" if it.trust == "untrusted" else None) if f)
                print(f"    {when:22} {it.kind:12} {it.title[:60]:60} {it.course or '':12} {flags}")
            if len(report.items) > 80:
                print(f"    ... and {len(report.items) - 80} more")
    if args.dry_run:
        print("\nDry run: nothing was saved.")
    return worst


def cmd_kpis(args: argparse.Namespace) -> int:
    from atlas.metrics import compute
    from atlas.store import get_engine

    print(json.dumps(compute(get_engine(), days=args.days), indent=2, default=str))
    return 0


def _safe_console() -> None:
    """Windows pipes default to cp1252; model replies and reports contain characters it can't encode (→, ·, emoji).
    Never let printing crash a run (the first full benchmark died on a '→' after finishing)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    p = argparse.ArgumentParser(prog="atlas", description="Atlas local assistant")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check the environment").set_defaults(func=cmd_doctor)
    sub.add_parser("seed-demo", help="load fictional demo data").set_defaults(func=cmd_seed_demo)
    sub.add_parser("clear-demo", help="remove demo data").set_defaults(func=cmd_clear_demo)
    a = sub.add_parser("ask", help="ask one question")
    a.add_argument("text", nargs="+")
    a.set_defaults(func=cmd_ask)
    sv = sub.add_parser("serve", help="run the local dashboard")
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--no-scheduler", action="store_true", help="don't run background sync / briefing jobs")
    sv.set_defaults(func=cmd_serve)
    br = sub.add_parser("brief", help="show a briefing now, or record whether today's was accurate")
    br.add_argument("--if-due", action="store_true", help="make today's daily briefing only if due and missing")
    br.add_argument("--no-sync", action="store_true", help="use stored data; don't sync first")
    br.add_argument("--notify", action="store_true", help="also show Windows notifications")
    br.add_argument("--verdict", choices=["accurate", "inaccurate"],
                    help="Phase 2 gate: did today's briefing match Canvas and your calendar?")
    br.add_argument("--missed", type=int, default=None, help="with --verdict: how many items it missed or got wrong")
    br.add_argument("--note", default=None, help="with --verdict: what was wrong")
    br.set_defaults(func=cmd_brief)
    b = sub.add_parser("bench", help="benchmark local models")
    b.add_argument("--models", nargs="+", default=["qwen3.5:4b", "qwen3:8b", "qwen3:4b"])  # slowest last
    b.add_argument("--ctx", nargs="+", type=int, default=[4096, 8192, 16384], help="num_ctx values to probe for VRAM")
    b.add_argument("--quick", action="store_true", help="fewer golden cases, one context size")
    b.add_argument("--now", default="2026-11-13T09:00:00",
                   help="reference date/time for the run (default: the test suite's Friday, so runs on different "
                        "days are comparable); 'live' = real clock")
    b.set_defaults(func=cmd_bench)
    au = sub.add_parser("auth", help="store credentials for a source (you type them; saved in the OS keyring)")
    au.add_argument("target", choices=["canvas", "gcal", "status", "forget"])
    au.add_argument("source", nargs="?", help="for `forget`: canvas or gcal")
    au.add_argument("--ics", action="store_true", help="use a calendar feed URL instead of the API")
    au.add_argument("--client-file", help="gcal: the Desktop-app OAuth client JSON you downloaded")
    au.set_defaults(func=cmd_auth)
    sy = sub.add_parser("sync", help="sync enabled sources into the local store")
    sy.add_argument("--source", choices=["routine", "canvas", "gcal"])
    sy.add_argument("--dry-run", action="store_true", help="fetch and print normalized items without saving")
    sy.add_argument("--force", action="store_true", help="ignore backoff / needs-reauth parking")
    sy.set_defaults(func=cmd_sync)
    k = sub.add_parser("kpis", help="print KPI baseline")
    k.add_argument("--days", type=int, default=7)
    k.set_defaults(func=cmd_kpis)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
