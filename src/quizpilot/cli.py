"""quizpilot command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from importlib import resources
from pathlib import Path

from .config import CONFIG_NAME, Config, load_config
from .kb import KB


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # Piped input (scripts, CI) would otherwise use the ANSI code page on
    # Windows; typing or pasting into the console is unaffected.
    try:
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _open_kb(cfg: Config) -> KB:
    return KB(cfg.resolve(cfg.kb.path))


def load_sites() -> list[dict]:
    text = resources.files("quizpilot").joinpath("data/sites.json").read_text(encoding="utf-8")
    return json.loads(text)


# --- commands -------------------------------------------------------------------


def cmd_init(args, cfg: Config) -> int:
    target = Path.cwd() / CONFIG_NAME
    if target.exists():
        print(f"{target} already exists.")
        return 0
    example = resources.files("quizpilot").joinpath("data/quizpilot.example.toml")
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Created {target}. Set DEEPSEEK_API_KEY and the model ID, then run `quizpilot chrome`.")
    return 0


def cmd_chrome(args, cfg: Config) -> int:
    from .browser import launch_browser

    launch_browser(cfg.browser.cdp_url, cfg.resolve(cfg.browser.profile_dir), args.exe or cfg.browser.executable or None)
    print(
        f"Chrome started with remote debugging at {cfg.browser.cdp_url}.\n"
        "Log in to the sites you need in THIS window (CNKI, Doubao, CNIPA, LeapSpace, ...).\n"
        "Logins persist in the profile folder for next time."
    )
    return 0


def cmd_ingest(args, cfg: Config) -> int:
    from .ingest import ingest_path

    with _open_kb(cfg) as kb:
        for p in args.paths:
            for f in ingest_path(kb, Path(p), module=args.module or ""):
                print(f"added {f}")
        print(kb.stats())
    return 0


def _module_filter(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {m.strip().zfill(2) for m in value.split(",") if m.strip()}


def cmd_sites(args, cfg: Config) -> int:
    wanted = _module_filter(args.modules)
    for entry in load_sites():
        if wanted and entry["module"] not in wanted:
            continue
        print(f"{entry['module']} {entry['title']}")
        for url in entry["urls"]:
            print(f"     {url}")
    return 0


def cmd_crawl(args, cfg: Config) -> int:
    from .browser import Browser, crawl

    wanted = _module_filter(args.modules)
    jobs: list[tuple[str, str]] = []
    # Explicit URLs alone crawl just those; otherwise the guide's sites.
    if wanted or not args.urls:
        jobs = [
            (e["module"], u)
            for e in load_sites()
            if not wanted or e["module"] in wanted
            for u in e["urls"]
        ]
    jobs += [(args.module or "", u) for u in args.urls]

    async def run() -> None:
        async with Browser(cfg.browser.cdp_url) as browser:
            with _open_kb(cfg) as kb:
                results = await crawl(browser, kb, jobs, cfg.resolve(cfg.kb.downloads), args.concurrency)
                ok = sum(r.startswith("ok") for r in results)
                print(f"\n{ok}/{len(results)} pages stored. KB: {kb.stats()}")

    asyncio.run(run())
    return 0


def cmd_capture(args, cfg: Config) -> int:
    from .browser import Browser

    async def run() -> None:
        async with Browser(cfg.browser.cdp_url) as browser:
            with _open_kb(cfg) as kb:
                print(await browser.capture_active(kb, module=args.module or "", note=args.note or ""))

    if not args.watch:
        asyncio.run(run())
        return 0
    print("Open a page or menu in Chrome, then press Enter here to capture it. Ctrl+C to stop.")
    try:
        while True:
            note = input("note (optional) > ").strip()
            args.note = note
            asyncio.run(run())
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


def cmd_search(args, cfg: Config) -> int:
    with _open_kb(cfg) as kb:
        for h in kb.search(" ".join(args.query), k=args.k, module=args.module):
            print(f"── {h.cite()}  score={h.score:.2f}")
            print(h.text[:400].strip(), "\n")
    return 0


def cmd_kb(args, cfg: Config) -> int:
    with _open_kb(cfg) as kb:
        print(kb.stats())
        if args.list:
            for source, title, kind, module in kb.documents(args.module):
                print(f"[{module or '--'}] {kind:7} {title[:50]}  <{source}>")
        if args.remove:
            kb.remove(args.remove)
            print(f"removed {args.remove}")
        for path in args.merge or []:
            try:
                counts = kb.merge_from(path)
            except ValueError as e:
                raise RuntimeError(str(e)) from e
            print(f"merged {path}: {counts}")
        if args.merge:
            print(kb.stats())
        if args.export:
            print(f"exported to {kb.export(args.export)} - send this file to your teammates")
    return 0


def cmd_pdf(args, cfg: Config) -> int:
    from . import pdftools

    src = args.file
    if src.startswith(("http://", "https://")):
        import httpx

        data = httpx.get(src, follow_redirects=True, timeout=60).content
        doc = pdftools.open_pdf(data)
    else:
        doc = pdftools.open_pdf(Path(src))
    with doc:
        info = pdftools.summary(doc)
        print(f"pages: {info['pages']}  title: {info['title']!r}  printed page labels: {info['has_labels']}")
        numbers: list[int] = []
        if args.label:
            n = pdftools.find_label(doc, args.label)
            if n is None:
                print(f"no page labelled {args.label!r}")
                return 1
            print(f"printed page {args.label} = PDF page {n}")
            numbers = [n]
        elif args.page:
            numbers = [p if p > 0 else doc.page_count + p + 1 for p in args.page]
        elif args.all:
            numbers = list(range(1, doc.page_count + 1))
        for n in numbers:
            pi = pdftools.page_info(doc[n - 1])
            print(
                f"\nPDF page {pi.number} (label {pi.label or '-'}): {pi.chars} chars, "
                f"{pi.images} images, {pi.drawings} vector drawings"
            )
            print(f"  first line: {pi.first_text[:80]}")
            print(f"  last line:  {pi.last_text[:80]}")
            print(f"  last char:  {pi.last_char}")
            if args.text:
                print(doc[n - 1].get_text())
            if args.render:
                out = pdftools.render_page(doc, n, cfg.resolve(cfg.kb.downloads) / f"page-{n}.png")
                print(f"  rendered:   {out}")
    return 0


def _read_question() -> str | None:
    print("\nPaste the question (options included), then an empty line.")
    print("Or just press Enter to use the clipboard. Ctrl+C to quit.")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            return "\n".join(lines) if lines else None
        if not line.strip():
            if lines:
                return "\n".join(lines)
            clip = _clipboard()
            if clip:
                print(clip)
                return clip
            print("(clipboard empty)")
            continue
        lines.append(line)


def _clipboard() -> str:
    from .console import read_clipboard

    return read_clipboard()


def _print_answer(q, ans, round_name: str, urls: list[str] | None = None, title: str = "") -> None:
    from .solver import break_even, worth_answering

    be = break_even(round_name)
    if ans.error:
        print(f"\n!! {ans.error}")
        return
    verdict = "ANSWER" if worth_answering(ans.confidence, round_name) else "SKIP (not worth the -1 risk)"
    print("\n" + "=" * 60)
    if title:
        print(f"  [{title}]")
    print(f"  {ans.answer or '?'}    confidence {ans.confidence:.2f}  ->  {verdict}")
    print(f"  (break-even {be:.2f} in the {round_name} round, {ans.seconds:.1f}s)")
    print("=" * 60)
    for letter, text in q.options.items():
        note = ans.options.get(letter, "")
        print(f"  {letter}. {text[:40]:<40} {note[:70]}")
    if ans.reason:
        print(f"  why: {ans.reason}")
    for h in ans.citations[:3]:
        print(f"  src: {h.cite()}")
    for url in (urls or [])[-3:]:
        print(f"  web: {url}")
    if not ans.citations and not urls:
        print("  src: none in the local KB - model knowledge only, verify if time allows")


def _model(cfg: Config):
    from .llm import OpenAICompatible

    return OpenAICompatible(cfg.llm)


LIVE_BUDGET = {"individual": 30, "team": 120}


def _budget(args) -> float:
    return args.budget or LIVE_BUDGET[args.round]


async def _answer_one(q, kb, model, cfg: Config, args, browser=None) -> None:
    from .solver import solve

    ans = await asyncio.to_thread(solve, q, kb, model, cfg.solver.top_k)
    if not args.live:
        _print_answer(q, ans, args.round)
        return
    # The instant local answer is a fallback you can use while the browser works.
    _print_answer(q, ans, args.round, title="quick answer from local knowledge")
    from .agent import Agent

    print(f"\nSearching live in Chrome (up to {_budget(args):.0f}s)...")
    agent = Agent(browser, model, load_sites(), kb, vision=cfg.llm.vision != "off", pointer=cfg.browser.pointer)
    result = await agent.run(q, budget=_budget(args))
    _print_answer(q, result.answer, args.round, urls=result.urls, title="live answer from the web")


def _use_watch(args) -> bool:
    """Clipboard watching in a real Windows console unless --paste is given."""
    if args.paste or sys.platform != "win32":
        return False
    from .console import has_console_input

    return has_console_input() or sys.stdin.isatty()


def _pasted_questions():
    while True:
        raw = _read_question()
        if raw is None:
            print("(input closed - exiting)")
            return
        yield raw


def _watch_clipboard(interval: float = 0.3):
    """Yield each newly copied question. Ctrl+C ends the session.

    A copy is detected by the clipboard sequence number where available, so
    copying the same question again answers it again.
    """
    from .console import clipboard_sequence, disable_quick_edit, looks_like_question, read_clipboard

    disable_quick_edit()
    print("\n>>> Ready. Copy a question with its options (Ctrl+C on the exam page).")
    print(">>> Answering starts automatically. Press Ctrl+C in this window to quit.", flush=True)
    last_seq = clipboard_sequence()
    last_text = read_clipboard()  # ignore whatever was copied before we started
    while True:
        time.sleep(interval)
        seq = clipboard_sequence()
        if seq is not None:
            if seq == last_seq:
                continue
            last_seq = seq
            text = read_clipboard()
        else:
            text = read_clipboard()
            if text == last_text:
                continue
        last_text = text
        if not text:
            continue
        if looks_like_question(text):
            print("\n" + "-" * 60 + f"\n{text}\n" + "-" * 60, flush=True)
            yield text
        else:
            preview = text.replace("\n", " ")[:40]
            print(f"(copied \"{preview}\" - not a question with options, ignored)", flush=True)


def cmd_ask(args, cfg: Config) -> int:
    from .browser import Browser
    from .question import parse_question

    from . import __version__

    mode = "copy-to-answer" if (not args.question and _use_watch(args)) else "paste"
    print(f"quizpilot {__version__} ({mode} mode)", flush=True)
    model = _model(cfg)
    # One event loop for the whole session, driven from the main thread.
    # Console input is read between questions, never from a worker thread
    # (on Windows that ended the program right after a paste).
    with asyncio.Runner() as runner, _open_kb(cfg) as kb:
        browser = None
        if args.live:
            from .browser import ensure_chrome

            if not ensure_chrome(cfg.browser.cdp_url, cfg.resolve(cfg.browser.profile_dir), executable=cfg.browser.executable):
                raise RuntimeError(f"Can't start Chrome at {cfg.browser.cdp_url}. Try `quizpilot chrome`.")
            browser = Browser(cfg.browser.cdp_url)
            runner.run(browser.__aenter__())
        try:
            if args.question:
                q = parse_question(" ".join(args.question), args.kind)
                runner.run(_answer_one(q, kb, model, cfg, args, browser))
                return 0
            questions = _watch_clipboard() if _use_watch(args) else _pasted_questions()
            for raw in questions:
                q = parse_question(raw, args.kind)
                print(f"-> {q.kind}, {len(q.options)} options", flush=True)
                try:
                    runner.run(_answer_one(q, kb, model, cfg, args, browser))
                except KeyboardInterrupt:
                    print("\n(stopped this question)")
                except Exception as e:  # keep the session alive for the next question
                    print(f"\n!! {type(e).__name__}: {e}")
                if _use_watch(args):
                    print("\n>>> Copy the next question (Ctrl+C here to quit).", flush=True)
        except KeyboardInterrupt:
            print()
        finally:
            if browser is not None:
                runner.run(browser.__aexit__(None, None, None))
    return 0


def cmd_eval(args, cfg: Config) -> int:
    from .browser import Browser
    from .question import parse_question, split_questions
    from .solver import ROUNDS, solve, worth_answering

    wanted = _module_filter(args.modules)
    questions = [parse_question(b) for b in split_questions(Path(args.file).read_text(encoding="utf-8"))]
    questions = [q for q in questions if q.expected and (not wanted or q.module in wanted)]
    if args.limit:
        questions = questions[: args.limit]
    model = _model(cfg)
    gain, loss = ROUNDS[args.round]
    stats = {"correct": 0, "answered": 0, "score": 0.0, "seconds": []}

    def record(i, q, ans) -> None:
        stats["seconds"].append(ans.seconds)
        ok = ans.answer == q.expected
        stats["correct"] += ok
        if worth_answering(ans.confidence, args.round):
            stats["answered"] += 1
            stats["score"] += gain if ok else loss
        mark = "OK " if ok else "BAD"
        note = f"  !! {ans.error[:60]}" if ans.error else ""
        print(
            f"{i:3} {mark} [{q.module or '--'}] got {ans.answer or '-':5} want {q.expected:5} "
            f"conf {ans.confidence:.2f} {ans.seconds:4.1f}s  {q.stem[:36]}{note}",
            flush=True,
        )

    async def run() -> None:
        with _open_kb(cfg) as kb:
            if not args.live:
                for i, q in enumerate(questions, 1):
                    record(i, q, await asyncio.to_thread(solve, q, kb, model, cfg.solver.top_k))
                return
            from .agent import Agent

            async with Browser(cfg.browser.cdp_url) as browser:
                for i, q in enumerate(questions, 1):
                    print(f"--- {i}/{len(questions)} [{q.module}] {q.stem[:50]}", flush=True)
                    agent = Agent(browser, model, load_sites(), kb, vision=cfg.llm.vision != "off", pointer=cfg.browser.pointer)
                    result = await agent.run(q, budget=_budget(args), close=True)
                    record(i, q, result.answer)

    asyncio.run(run())
    n = len(stats["seconds"])
    if n:
        print(
            f"\n{stats['correct']}/{n} correct ({stats['correct'] / n:.0%}); answering only when worth it: "
            f"{stats['answered']} answered, score {stats['score']:+.0f} ({args.round} scoring); "
            f"avg {sum(stats['seconds']) / n:.1f}s per question"
        )
    return 0


def cmd_ui(args, cfg: Config) -> int:
    from .ui import App

    with _open_kb(cfg) as kb:
        app = App(cfg, kb, _model(cfg), load_sites(), live_available=not args.no_live)
        app.serve(port=args.port, open_browser=not args.no_open)
    return 0


def cmd_samples(args, cfg: Config) -> int:
    from .samples import extract_samples, to_practice_text

    samples = extract_samples(Path(args.pdf))
    Path(args.out).write_text(to_practice_text(samples), encoding="utf-8")
    print(f"wrote {len(samples)} sample questions to {args.out}")
    return 0


# --- argument parsing ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    p = argparse.ArgumentParser(prog="quizpilot", description="Research co-pilot for the AI+ information literacy contest")
    p.add_argument("--version", action="version", version=f"quizpilot {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create quizpilot.toml in this folder").set_defaults(fn=cmd_init)

    s = sub.add_parser("chrome", help="start Chrome/Edge with remote debugging and a persistent profile")
    s.add_argument("--exe", help="path to chrome.exe or msedge.exe")
    s.set_defaults(fn=cmd_chrome)

    s = sub.add_parser("ingest", help="add local PDFs / HTML / text files or folders to the knowledge base")
    s.add_argument("paths", nargs="+")
    s.add_argument("--module", help="module number to tag, e.g. 11")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("sites", help="list the contest sites per module")
    s.add_argument("--modules", help="comma-separated module numbers")
    s.set_defaults(fn=cmd_sites)

    s = sub.add_parser("crawl", help="snapshot contest sites (or given URLs) into the knowledge base")
    s.add_argument("urls", nargs="*", help="extra URLs to fetch")
    s.add_argument("--modules", help="only these modules, e.g. 11,12,21 (default: all)")
    s.add_argument("--module", help="module tag for the extra URLs")
    s.add_argument("--concurrency", type=int, default=4)
    s.set_defaults(fn=cmd_crawl)

    s = sub.add_parser("capture", help="store the current Chrome tab (e.g. an opened menu) in the knowledge base")
    s.add_argument("--module")
    s.add_argument("--note", help="what this capture shows, e.g. '豆包 PPT 风格选项'")
    s.add_argument("--watch", action="store_true", help="keep capturing each time you press Enter")
    s.set_defaults(fn=cmd_capture)

    s = sub.add_parser("search", help="raw knowledge-base search")
    s.add_argument("query", nargs="+")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--module")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("kb", help="knowledge-base stats, listing, removal and team sharing")
    s.add_argument("--list", action="store_true")
    s.add_argument("--module")
    s.add_argument("--remove", metavar="SOURCE")
    s.add_argument("--merge", nargs="+", metavar="FILE", help="merge teammates' exported KB files into yours")
    s.add_argument("--export", metavar="FILE", help="write your KB to one file to share")
    s.set_defaults(fn=cmd_kb)

    s = sub.add_parser("pdf", help="page-accurate PDF facts: page count, images, last character, labels")
    s.add_argument("file", help="path or URL")
    s.add_argument("--page", type=int, nargs="*", help="PDF page numbers; negative counts from the end (-2 = second to last)")
    s.add_argument("--label", help="printed page number to locate (e.g. 50)")
    s.add_argument("--all", action="store_true", help="show every page")
    s.add_argument("--text", action="store_true", help="print the page text")
    s.add_argument("--render", action="store_true", help="save the page as PNG")
    s.set_defaults(fn=cmd_pdf)

    for name, fn, help_ in (
        ("ask", cmd_ask, "answer questions from the knowledge base (contest mode)"),
        ("eval", cmd_eval, "measure accuracy and score on a practice file"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--round", choices=["individual", "team"], default="individual")
        s.add_argument("--live", action="store_true", help="also research live in your Chrome (start it with `quizpilot chrome`)")
        s.add_argument("--budget", type=float, help="seconds per question for --live (default 30 individual, 120 team)")
        if name == "ask":
            s.add_argument("question", nargs="*", help="question text (omit for interactive mode)")
            s.add_argument("--kind", choices=["single", "multi", "judge"], help="override the detected type")
            s.add_argument("--paste", action="store_true", help="type/paste questions here instead of watching the clipboard")
        else:
            s.add_argument("file")
            s.add_argument("--modules", help="only these modules, e.g. 11,12,24")
            s.add_argument("--limit", type=int, help="only the first N questions")
        s.set_defaults(fn=fn)

    s = sub.add_parser("ui", help="open the answering page in your browser (paste questions there)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true", help="don't open the page automatically")
    s.add_argument("--no-live", action="store_true", help="local answers only, no Chrome research")
    s.set_defaults(fn=cmd_ui)

    s = sub.add_parser("samples", help="extract the guide's sample questions into a practice file")
    s.add_argument("pdf")
    s.add_argument("--out", default="practice.txt")
    s.set_defaults(fn=cmd_samples)
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    cfg = load_config()
    try:
        return args.fn(args, cfg) or 0
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
