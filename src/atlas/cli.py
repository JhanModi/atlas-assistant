"""atlas: command-line entry point.

  atlas doctor                  check Python, config, Ollama, models, GPU
  atlas seed-demo | clear-demo  load/remove fictional demo data (until real connectors exist)
  atlas ask "what's due?"       one question through the full pipeline
  atlas serve                   local dashboard on http://127.0.0.1:8765
  atlas bench [--models ...]    benchmark local models (VRAM, speed, tool-call validity, latency)
  atlas kpis [--days 7]         print the KPI baseline
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
    from atlas.web.app import create_app

    s = get_settings()
    host = s.web.host
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"Refusing to bind to {host}: the dashboard has no login. Use 127.0.0.1.")
        return 2
    print(f"Atlas dashboard: http://{host}:{args.port or s.web.port}")
    uvicorn.run(create_app(), host=host, port=args.port or s.web.port, log_level="warning")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from atlas.evaluation.bench import run_bench

    return run_bench(models=args.models, contexts=args.ctx, quick=args.quick)


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
    sv.set_defaults(func=cmd_serve)
    b = sub.add_parser("bench", help="benchmark local models")
    b.add_argument("--models", nargs="+", default=["qwen3.5:4b", "qwen3:8b", "qwen3:4b"])  # slowest last
    b.add_argument("--ctx", nargs="+", type=int, default=[4096, 8192, 16384], help="num_ctx values to probe for VRAM")
    b.add_argument("--quick", action="store_true", help="fewer golden cases, one context size")
    b.set_defaults(func=cmd_bench)
    k = sub.add_parser("kpis", help="print KPI baseline")
    k.add_argument("--days", type=int, default=7)
    k.set_defaults(func=cmd_kpis)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
