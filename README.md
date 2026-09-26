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

Everything below can also be done from the answering page's **知识库** tab,
without the terminal: pick modules to crawl (progress is shown live), save
the page currently open in the dedicated browser, import PDF/HTML/TXT files,
export your knowledge base and merge a teammate's.

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

Crawling opens a few tabs at a time but only one per site, with a gap
between requests to the same site (bursts are the usual trigger for
verification pages), and blocks images and fonts to load faster.
quizpilot never tries to solve CAPTCHAs itself: that breaks the sites' terms
and can get an account or a school's IP blocked. When one appears, the
answering page shows a banner, flashes its tab title and sends a system
notification, and research resumes as soon as you've solved it. If a page is stuck behind a CAPTCHA, it beeps, brings that tab to
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

## Answering page (easiest)

Double-click **`quizpilot-ui.bat`**. Your browser opens a local page
(only this computer can reach it) and the dedicated Chrome starts by
itself. Paste a question with its options, press **开始答题** or Ctrl+Enter,
and watch:

1. the instant local answer,
2. each step of the live research in Chrome,
3. the final answer with confidence, answer/skip advice and source links.

While it researches, each question has buttons:

- **我来操作** pauses the agent (even mid-way through a slow page) so you
  can use the browser yourself: wait for a page, open the right record,
  try another keyword. **继续作答** hands back; the agent carries on from
  the tab you left in front and reads that page first.
- **停止，马上作答** stops searching and answers from what it has seen.
- **继续查找** (after it finishes) researches for another round from the
  current page, remembering what it already found.

Time spent paused or on a verification page doesn't count against the
time limit. Several questions can run at once, which suits the team round.
From a terminal the same page is `quizpilot ui`.

The **网站登录** tab lists the contest sites (grouped like the bookmarks
folder), marking the ones that need an account. Click 打开登录 to open a
site in the dedicated browser, log in there, then click 标记已登录. The
browser profile keeps the login for next time, and the research agent
prefers sites you're logged in to. Add new sites there, or import a
bookmarks file exported from Chrome or Edge.

The **操作流程** tab records multi-step operations once and replays them.
Enter a start URL (for example CNKI advanced search), click 开始录制, and
do the task in the browser window: every click, typed value, dropdown
choice and checkbox is captured, including in tabs the site opens. Name the
flow and save it. Typed values and dropdown choices become parameters.
During live research the model sees matching flows and can run one as a
single action with this question's values, so a 10-click CNKI sequence
takes two model calls instead of ten. Flows are stored in the knowledge
base and shared with `kb --export` / `--merge`. Use 试运行 to check a flow
with other values. If a site changes its layout, record the flow again.

The dedicated browser is Chrome if found, otherwise Edge; both work. To
choose, set `executable` under `[browser]` in `quizpilot.toml`. Stick to
one: logins saved by Edge can't be read by Chrome.

## Contest mode

```powershell
quizpilot ask --round individual
```

On Windows, just copy the question with its options on the exam page
(Ctrl+C): answering starts automatically, with nothing to paste into the
console. Use `--paste` to type or paste questions into the console instead.

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

## Live research in your browser

With `--live`, quizpilot also researches in your own Chrome (start it with
`quizpilot chrome` first). Like a person, it opens the official site from
the guide, types into search boxes, clicks results, picks dropdown
options, finds text on long pages and reads PDFs page by page. You can
watch it work in the browser window.

```powershell
quizpilot ask --live --round team            # up to 120 s per question
quizpilot ask --live --budget 40             # custom time limit
quizpilot eval practice.txt --live --modules 11,12,24 --budget 60
```

It prints the instant local answer first, so you have a fallback while the
browser works.

How it reads and works a page:

- The page is shown to the model as text in reading order with each
  clickable element numbered in place (`GB/T 38880-2020 … [12]<a>详情</a>`),
  so it clicks the link on the right result's line. Numbers stay the same
  for the same element while it stays on the page.
- Pop-up dialogs are shown first; long menus are moved to the end.
- Script-driven widgets (custom dropdowns, tabs, pagers) count as
  clickable too, and it can click by visible text.
- It scrolls up and down (including scrollable panels inside a page, which
  load more as you scroll), turns result pages with `next_page`, hovers
  menus, and reads long pages chunk by chunk.
- After every click it's told whether the page changed, so a wrong click
  isn't mistaken for progress. After a page turn it waits for results that
  are still showing 正在加载….
- The evidence it quotes for an answer is checked against the pages it
  actually read; a quote that isn't there is sent back once to verify, and
  otherwise caps the confidence at 0.6.

Each step the model may chain up to three actions (for example type the
query and click 检索), and it reads search forms inside iframes. When it answers a
question with confidence 0.7 or more, the steps are saved as a recipe in
the knowledge base; similar questions later start from that recipe, and
recipes travel to teammates with `kb --export` / `--merge`. Every page and PDF it reads is saved to the knowledge base,
so questions it has researched before answer faster next time. If a site
shows a CAPTCHA or a login page, it beeps and waits for you.

### Vision

If the model accepts images, the agent can also look at the page: `look`
answers a question from a screenshot (charts such as CNKI's 可视化分析,
icon-only buttons, layout), `click_xy` clicks a spot found that way, and a
PDF page can be looked at ("how many figures on page 50"). Reading the page
structure stays the default because it is cheaper and exact; vision covers
what text can't. With `vision = "auto"` (the default, under `[llm]`), the
first rejected image switches vision off for the session and research goes
on text-only. Images load normally while vision is on so screenshots
aren't blank.

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

- **Site adapters** that fill advanced-search forms exactly (CNKI, Wanfang,
  CQVIP, CNIPA, openstd) for result-count questions.
- **A vision model** for "how many figures on page N" questions.
