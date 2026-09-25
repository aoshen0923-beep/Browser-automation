# quizpilot

A research co-pilot for the 2026 "AI+信息素养" contest, built for Windows,
Chrome/Edge and DeepSeek.

## Why it works this way

The elimination round (淘汰赛) gives **30 seconds per question** in the
individual round (10 questions / 5 min, +2 / −1) and 90 seconds in the team
round (10 / 15 min, +4 / −1). A browser agent that clicks through CNKI or
CNIPA live can't finish in 30 seconds, and a wrong answer costs a point.
So quizpilot does the slow work **before** the contest:

1. **Prep (days before):** crawl the ~200 sites from the prep guide in your
   own logged-in Chrome, capture the menus/filters/feature panels that
   questions ask about, and ingest the regulations, standards and PDFs the
   modules list, all into a local full-text knowledge base.
2. **Contest:** copy a question, press Enter, and within a few seconds get
   an answer that checks each option against the local evidence. It shows
   a confidence score, the source and page, and whether answering beats
   skipping under the round's scoring.

You make the final call and click the answer yourself. quizpilot never
touches the exam page.

> Check the exam-day rules before relying on this in the contest itself.
> Either way it's a practice tool: `quizpilot eval` scores you (and it)
> on practice questions.

## Setup (Windows)

1. Install Python 3.11+ from python.org and tick "Add to PATH".
2. In PowerShell, from this folder, run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
.\.venv\Scripts\Activate.ps1
setx DEEPSEEK_API_KEY "sk-..."      # then open a new terminal
```

3. `quizpilot.toml` uses `deepseek-flash` by default. Model names are
   lowercase; the API's error message lists the valid ones if a name is wrong.

## Team setup (3 people)

Each teammate runs quizpilot on their own Windows PC with their own Chrome
logins. The whole team shares one knowledge base.

**Install for teammates, no Python needed.** Every push builds
`quizpilot-windows.zip` on GitHub (Actions → windows-build → Artifacts). To
publish a release with a permanent link: Actions → windows-build → Run
workflow → enter a new version such as `v0.1.1`. To use it:

1. Right-click the zip → **Extract All** (全部解压缩). Don't run anything
   from inside the zip without extracting it: Windows then unpacks only
   the file you clicked, and `quizpilot` won't be found.
2. Double-click `quizpilot-shell.bat`. This opens a terminal where
   `quizpilot` works and creates `quizpilot.toml` the first time.
3. Set the API key: `setx DEEPSEEK_API_KEY "sk-..."`, then reopen the shell.
   Everyone can use the same DeepSeek key.
4. Run `quizpilot chrome` and log in with **your own** accounts. Never share
   Chrome profiles: they contain your login cookies.

Permanent download link for the latest version:
https://github.com/aoshen0923-beep/Browser-automation/releases/latest

**Split the prep, then merge.** Divide the 50 modules, for example:

| Person | Modules |
|---|---|
| A | 01–17 (AI tools, government sites, law, standards, patents) |
| B | 18–34 (learning resources, academic databases, search techniques) |
| C | 35–50 (filters, search engines, tools, academic writing) |

Each person crawls, captures and ingests their own modules, then exports:

```powershell
quizpilot kb --export kb-A.sqlite
```

One person merges all three exports and sends the combined file back to
everyone, who merges it in turn:

```powershell
quizpilot kb --merge kb-B.sqlite kb-C.sqlite
quizpilot kb --export kb-team.sqlite
quizpilot kb --merge kb-team.sqlite
```

Merging is safe to repeat. When two people captured the same page, the
newer capture wins. Anything you captured that a teammate didn't is kept.

**Team round (10 questions, 15 minutes, +4/−1).** Everyone runs
`quizpilot ask --round team`. Split the questions (for example A: 1–4,
B: 5–7, C: 8–10), and let whoever finishes first double-check the
low-confidence answers of the others. At +4/−1, even a single-choice guess
with one wrong option ruled out is worth answering.

## Prep workflow

```powershell
quizpilot chrome
```

This starts Chrome (or Edge) with a dedicated profile. Log in once to CNKI,
Wanfang, Doubao, LeapSpace, the CNIPA patent system, Baidu Index, AI 知数 and
the other sites that need an account. The logins persist.

```powershell
quizpilot sites --modules 11,12          # see what the guide lists per module
quizpilot crawl --modules 11,12,21       # snapshot those sites into the KB
quizpilot crawl https://example.org/x.pdf --module 01   # extra pages/PDFs
```

Crawling opens a few tabs at a time and blocks images and fonts to load
faster. If a page is stuck behind a CAPTCHA, it beeps, brings that tab to
the front and waits for you to solve it.

Many questions ask what a menu or panel contains, for example "which styles
does Doubao's PPT generator offer" or "which filters does Wiley show". Open
that menu yourself, then capture it:

```powershell
quizpilot capture --watch --module 04    # press Enter after each menu you open
```

To add documents you downloaded (regulations, standards, the prep guide):

```powershell
quizpilot ingest D:\contest\regulations --module 01
quizpilot ingest D:\contest\guide.pdf
```

To check the knowledge base:

```powershell
quizpilot kb --list
quizpilot search 儿童口罩 起草人
```

## Practice and measure

```powershell
quizpilot samples guide.pdf --out practice.txt   # the guide's 55 sample questions
quizpilot eval practice.txt --round individual
```

`eval` prints accuracy, the round score you'd get if you only answered when
the tool says so, and the average seconds per question. Add your own
practice questions to the file, separated by `---` lines and ending with
`正确答案：...`.

## Contest mode

```powershell
quizpilot ask --round individual
```

Copy the question and its options from the exam page, then press Enter on
an empty line to read the clipboard. You can also paste the text directly.

```
============================================================
  C    confidence 0.86  ->  ANSWER
  (break-even 0.33 in the individual round, 2.4s)
============================================================
  A. 高尚荣          true: listed as a drafter
  C. 许伟民          false: not in the drafter list
  src: GB/T 38880-2020 儿童口罩技术规范 p.2 <...>
```

**When to answer.** Answer when your chance of being right is above
1/3 in the individual round (+2/−1) or above 1/5 in the team round (+4/−1).
That means:

- A blind guess on a true/false question pays off in both rounds.
- A blind guess on a 4-option single choice loses points in the individual
  round but pays off in the team round.
- Blind guesses on multi-select questions don't pay off.

## PDF facts without guessing

```powershell
quizpilot pdf book.pdf --label 50 --render   # printed page 50 -> PDF page, image count, PNG
quizpilot pdf std.pdf --page -2              # second-to-last page: last line and last character
quizpilot pdf https://arxiv.org/pdf/2108.09800 # page count
```

## Layout

| Path | What it is |
|---|---|
| `src/quizpilot/kb.py` | SQLite FTS5 knowledge base (Chinese bigram tokenization) |
| `src/quizpilot/browser.py` | Attach to your Chrome over CDP, crawl, capture, CAPTCHA pause |
| `src/quizpilot/solver.py` | Option-by-option answering with confidence and the answer/skip rule |
| `src/quizpilot/question.py` | Parses pasted questions (single/multi/judge, A–D options, answer key) |
| `src/quizpilot/pdftools.py` | Page-accurate PDF facts |
| `src/quizpilot/data/sites.json` | The guide's sites per module (edit freely) |
| `scripts/quizpilot.spec`, `.github/workflows/windows-build.yml` | Windows build without Python (PyInstaller) |

Run the tests with `pytest`.

## Roadmap

- **Live research agent for the team round:** 90 seconds per question and
  three people make live lookups worthwhile.
- **Site adapters** that fill advanced-search forms exactly (CNKI, Wanfang,
  CQVIP, CNIPA, openstd) for result-count questions.
- **A vision model** for "how many figures on page N" questions.
