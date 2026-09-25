"""Live research agent: drives your own Chrome like a person would.

Each step the model sees the current page (numbered clickable/typeable
elements plus visible text) and picks one action: open a URL, search,
click, type, pick a dropdown option, find text on the page, read a PDF,
go back, or answer. Every page it reads is also saved to the knowledge
base, so the tool gets faster on questions it has researched before.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urljoin

from playwright.async_api import Page

from . import pdftools
from .browser import BLOCKED_RESOURCES, Browser, page_blocked, wait_for_human
from .kb import KB
from .llm import ChatModel, LLMError
from .question import JUDGE, MULTI, SINGLE, Question
from .solver import Answer, normalize_answer

# Marks visible interactive elements with data-qp="N" and returns a compact
# listing, so the model can say "click 12" instead of guessing selectors.
SNAPSHOT_JS = r"""
({maxItems, maxText, prefix}) => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  document.querySelectorAll('[data-qp]').forEach(e => e.removeAttribute('data-qp'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],' +
              '[role=tab],[role=menuitem],[role=option],[role=checkbox],[onclick],summary';
  const all = [...document.querySelectorAll(sel)].filter(visible);
  // Too many elements: keep form fields first, then content, then site chrome
  // (nav/header/footer), so search results aren't cut off by menus.
  const rank = el => {
    const t = el.tagName;
    if (t === 'INPUT' || t === 'SELECT' || t === 'TEXTAREA' || t === 'BUTTON') return 0;
    return el.closest('nav,header,footer,[role=navigation],[role=banner],[role=contentinfo]') ? 2 : 1;
  };
  let chosen = all;
  if (all.length > maxItems) {
    const keep = new Set(all.map((el, i) => [rank(el), i, el]).sort((a, b) => a[0] - b[0] || a[1] - b[1])
      .slice(0, maxItems).map(x => x[2]));
    chosen = all.filter(el => keep.has(el));
  }
  const items = [];
  let n = 0;
  for (const el of chosen) {
    n++;
    const ref = prefix + n;
    el.setAttribute('data-qp', ref);
    const tag = el.tagName.toLowerCase();
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                   el.getAttribute('title') || el.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 50);
    let extra = '';
    if (tag === 'input') extra = ' type=' + (el.type || 'text') + (el.checked ? ' checked' : '');
    if (tag === 'select') extra = ' options=' + [...el.options].map(o => o.text.trim()).slice(0, 12).join('|');
    if (tag === 'a') {
      const h = el.getAttribute('href') || '';
      if (h && !h.startsWith('javascript') && !h.startsWith('#')) extra = ' -> ' + h.slice(0, 90);
    }
    items.push(`[${ref}] ${tag}${extra} "${label}"`);
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n\s*\n+/g, '\n');
  return {title: document.title, url: location.href, items, text: text.slice(0, maxText),
          textLength: text.length, omitted: all.length - chosen.length};
}
"""

ACTIONS = """每一步输出一个JSON对象：
{"memory":"一两句话：已经确认了什么、下一步打算","actions":[动作1, 动作2, ...]}
actions 最多3个，按顺序执行；会改变页面的动作（goto/search/click/back/带回车的type）之后的动作不再执行，
所以把"输入+点击检索"这类组合放在同一步里能省时间。可用动作：
{"action":"goto","url":"https://..."}                 打开网址（优先用下面目录里的官方网站）
{"action":"search","query":"...","engine":"bing"}     用搜索引擎搜索（engine 可选 bing / baidu）
{"action":"click","ref":12}                          点击编号为12的元素（内嵌框架里的元素编号形如 "2-5"）
{"action":"type","ref":5,"text":"...","enter":true}  在输入框5输入文字，enter=true 表示输入后按回车
{"action":"select","ref":7,"option":"学位论文"}         在下拉框7中选择一个选项
{"action":"find","text":"起草人"}                      在当前页面全文中查找关键词（页面很长时用）
{"action":"pdf","url":"(可省略=当前页)","page":2,"find":"关键词"}  读取PDF：页数、指定页（负数从末尾算，-2=倒数第二页）、或查找关键词
{"action":"back"}                                    返回上一页
{"action":"answer","answer":"C","confidence":0.9,"reason":"一句话","evidence":"页面上看到的原文"}
answer 的依据必须来自页面上看到的内容；没看到的不要编造，用较低的 confidence 表示。"""

SYSTEM_TEMPLATE = """你在操作用户的Chrome浏览器，为信息素养大赛查找客观题的答案。像熟练的真人一样高效操作。
规则：
1. 优先直接打开下面目录中对应模块的官方网站，不要先用搜索引擎；目录里没有的网站再用搜索。
2. 很多网站可以直接在URL里带检索词；知道就直接用，省去点击。
3. 找到能判断答案的原文后立刻 answer，不要多余操作。每个选项都要核对。
4. 不要重复同一个失败的动作；元素编号每一步都会刷新，只用最新的编号。
5. 页面需要登录或出现验证码时，程序会暂停等用户处理，你继续即可。
6. answer 不能为空：单选一个字母，多选2-4个字母，判断题"对"或"错"。时间不够时也要给出最可能的答案，用 confidence（0-1，诚实）表示把握。
{actions}

网站目录（模块：网址）：
{directory}"""


def suggest_sites(text: str, sites: list[dict], k: int = 3) -> list[dict]:
    """Modules whose keywords/title best match the question (rarer words count more)."""
    import math

    from .textutil import tokens

    docs = [set(tokens(s.get("keywords", "") + " " + s["title"])) for s in sites]
    df: dict[str, int] = {}
    for toks in docs:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    q = set(tokens(text))
    scored = []
    for s, toks in zip(sites, docs):
        if not s["urls"]:
            continue
        score = sum(math.log(1 + len(sites) / df[t]) for t in q & toks)
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:k]]


def site_directory(sites: list[dict]) -> str:
    return "\n".join(f"{s['module']} {s['title']}: {' '.join(s['urls'])}" for s in sites if s["urls"])


@dataclass
class Step:
    action: dict
    result: str


@dataclass
class LiveResult:
    answer: Answer
    steps: list[Step] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"…(共{len(text)}字，用 find 查找)"


class Agent:
    def __init__(
        self,
        browser: Browser,
        model: ChatModel,
        sites: list[dict],
        kb: KB | None = None,
        *,
        max_items: int = 70,
        max_text: int = 2500,
        log=print,
        logged_in: list[dict] | None = None,
    ):
        self.browser = browser
        self.model = model
        self.kb = kb
        self.sites = sites
        self.system = SYSTEM_TEMPLATE.format(actions=ACTIONS, directory=site_directory(sites))
        if logged_in:
            self.system += "\n\n用户已在这个浏览器中登录的网站（需要账号的内容优先用这些，不用再登录）：\n" + "\n".join(
                f"- {x['name']}: {x['url']}" for x in logged_in
            )
        self.max_items = max_items
        self.max_text = max_text
        self.log = log
        self.page: Page | None = None
        self._pdf_cache: dict[str, bytes] = {}
        self._frames: dict[int, object] = {}
        self._memory = ""
        self._recipes: list[str] = []
        self._saved: set[str] = set()

    # --- page handling -------------------------------------------------------------

    async def _open_tab(self) -> Page:
        page = await self.browser.context.new_page()
        await self._prepare(page)
        return page

    async def _prepare(self, page: Page) -> None:
        async def handler(route):
            if route.request.resource_type in BLOCKED_RESOURCES:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", handler)

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass  # pages with polling never go idle; the DOM is enough
        if await page_blocked(self.page):
            await self.page.unroute("**/*")  # the CAPTCHA picture must load
            await self.page.reload(wait_until="domcontentloaded")
            await wait_for_human(self.page, log=self.log)
            await self._prepare(self.page)

    async def _follow_new_tab(self, before: int) -> None:
        pages = self.browser.context.pages
        if len(pages) > before:
            self.page = pages[-1]
            await self._prepare(self.page)
            await self.page.bring_to_front()

    async def _content_frames(self) -> list:
        """Visible, reasonably sized iframes (search forms often live in one)."""
        frames = []
        for frame in self.page.frames[1:]:
            if len(frames) >= 3:
                break
            try:
                el = await frame.frame_element()
                box = await el.bounding_box()
            except Exception:
                continue
            if box and box["width"] >= 200 and box["height"] >= 100:
                frames.append(frame)
        return frames

    async def observe(self) -> str:
        page = self.page
        if self._is_pdf_url(page.url):
            return f"当前页是PDF：{page.url}\n用 pdf 动作读取。"
        try:
            snap = await page.evaluate(SNAPSHOT_JS, {"maxItems": self.max_items, "maxText": self.max_text, "prefix": ""})
        except Exception as e:
            return f"读取页面失败：{e}"
        self._frames = {}
        parts = [
            f"标题：{snap['title']}\n网址：{snap['url']}\n可操作元素：\n" + "\n".join(snap["items"]),
        ]
        if snap.get("omitted"):
            parts.append(f"（另有{snap['omitted']}个导航/页脚元素未列出）")
        texts = [snap["text"]]
        for i, frame in enumerate(await self._content_frames(), start=1):
            try:
                fs = await frame.evaluate(SNAPSHOT_JS, {"maxItems": 30, "maxText": 1200, "prefix": f"{i}-"})
            except Exception:
                continue
            self._frames[i] = frame
            parts.append(f"\n内嵌框架{i}（{fs['url'][:80]}）的元素：\n" + "\n".join(fs["items"]))
            texts.append(f"[内嵌框架{i}] {fs['text']}")
            self._save(fs["url"], fs["title"], fs["text"])
        self._save(snap["url"], snap["title"], snap["text"])
        body = "\n".join(texts)
        parts.append(f"\n页面文字（主页面共{snap['textLength']}字，这里是开头部分，长页面用 find 查找）：\n{body}")
        return "\n".join(parts)

    def _save(self, url: str, title: str, text: str) -> None:
        if self.kb is None or not text.strip() or url in self._saved or url.startswith(("about:", "chrome")):
            return
        self._saved.add(url)
        try:
            self.kb.add_document(url, [(None, text)], title=title, kind="live", module="live")
        except Exception:
            pass

    @staticmethod
    def _is_pdf_url(url: str) -> bool:
        return bool(re.search(r"\.pdf($|[?#])", url, re.I)) or "/pdf/" in url.lower()

    def _element(self, ref: object):
        ref = str(ref).strip().strip("[]@")
        if "-" in ref:  # "2-5" = element 5 inside iframe 2
            frame_no = int(ref.split("-", 1)[0])
            frame = self._frames.get(frame_no)
            if frame is None:
                raise ValueError(f"没有内嵌框架{frame_no}，请用最新的元素编号")
            return frame.locator(f'[data-qp="{ref}"]').first
        return self.page.locator(f'[data-qp="{int(ref)}"]').first

    # --- actions -------------------------------------------------------------------

    async def act(self, a: dict) -> str:
        kind = a.get("action")
        page = self.page
        before = len(self.browser.context.pages)
        if kind == "goto":
            url = str(a.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            ctype = (resp.headers.get("content-type", "") if resp else "").lower()
            if "pdf" in ctype:
                return await self.read_pdf({"url": url})
            await self._settle()
            return "已打开"
        if kind == "search":
            q = quote_plus(str(a.get("query", "")))
            engine = str(a.get("engine", "bing")).lower()
            url = f"https://www.baidu.com/s?wd={q}" if engine == "baidu" else f"https://cn.bing.com/search?q={q}"
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await self._settle()
            return "已搜索"
        if kind == "click":
            el = self._element(a.get("ref"))
            href = await el.get_attribute("href")
            try:
                await el.click(timeout=5000)
            except Exception:
                await el.evaluate("e => e.click()")
            await asyncio.sleep(0.6)
            await self._follow_new_tab(before)
            if href and self._is_pdf_url(urljoin(page.url, href)):
                return await self.read_pdf({"url": urljoin(page.url, href)})
            await self._settle()
            return "已点击"
        if kind == "type":
            el = self._element(a.get("ref"))
            await el.fill(str(a.get("text", "")), timeout=5000)
            if a.get("enter"):
                await el.press("Enter")
                await asyncio.sleep(0.6)
                await self._follow_new_tab(before)
                await self._settle()
            return "已输入"
        if kind == "select":
            el = self._element(a.get("ref"))
            option = str(a.get("option", ""))
            try:
                await el.select_option(label=option, timeout=5000)
            except Exception:
                await el.select_option(value=option, timeout=5000)
            await self._settle()
            return "已选择"
        if kind == "find":
            return await self.find_text(str(a.get("text", "")))
        if kind == "pdf":
            return await self.read_pdf(a)
        if kind == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
            await self._settle()
            return "已返回"
        return f"未知动作：{kind}"

    async def find_text(self, needle: str) -> str:
        if not needle:
            return "find 需要 text"
        text = ""
        for frame in [self.page.main_frame, *self._frames.values()]:
            try:
                text += "\n" + await frame.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        hits = []
        for i, ln in enumerate(lines):
            if needle.lower() in ln.lower():
                ctx = " / ".join(lines[max(0, i - 1) : i + 2])
                hits.append(ctx[:300])
            if len(hits) >= 15:
                break
        return f"找到{len(hits)}处“{needle}”：\n" + "\n".join(hits) if hits else f"页面中没有“{needle}”"

    async def read_pdf(self, a: dict) -> str:
        url = str(a.get("url") or self.page.url)
        if url not in self._pdf_cache:
            resp = await self.browser.context.request.get(url, timeout=60000)
            self._pdf_cache[url] = await resp.body()
        try:
            doc = pdftools.open_pdf(self._pdf_cache[url])
        except Exception:
            return f"{url} 不是可读取的PDF"
        with doc:
            total = doc.page_count
            texts = pdftools.page_texts(doc)
            if self.kb is not None and url not in self._saved:
                self._saved.add(url)
                self.kb.add_document(url, texts, title=url.rsplit("/", 1)[-1], kind="pdf", module="live")
            out = [f"PDF {url}：共{total}页"]
            needle = str(a.get("find") or "")
            if needle:
                found = [(n, t) for n, t in texts if needle in t]
                out.append(f"“{needle}”出现在第 {[n for n, _ in found][:20]} 页")
                for n, t in found[:3]:
                    i = t.find(needle)
                    out.append(f"第{n}页：…{t[max(0, i - 150): i + 250]}…")
            page_no = a.get("page")
            if page_no is None and a.get("label"):
                page_no = pdftools.find_label(doc, str(a["label"]))
            if page_no is not None:
                n = int(page_no)
                n = n if n > 0 else total + n + 1
                if 1 <= n <= total:
                    info = pdftools.page_info(doc[n - 1])
                    out.append(
                        f"第{n}页（印刷页码 {info.label or '-'}）：{info.images}张图片，最后一个字“{info.last_char}”\n"
                        + _clip(doc[n - 1].get_text(), 3000)
                    )
            elif not needle:
                out.append("第1页：\n" + _clip(texts[0][1] if texts else "", 1500))
            return "\n".join(out)

    # --- the loop ------------------------------------------------------------------

    def _prompt(self, q: Question, steps: list[Step], observation: str, remaining: float, force: bool) -> str:
        kind = {SINGLE: "单选题", MULTI: "多选题（2-4个正确答案）", JUDGE: "判断题（回答 对/错）"}[q.kind]
        parts = [f"题型：{kind}", f"题目：{q.stem}"]
        parts += [f"{k}、{v}" for k, v in q.options.items()]
        hints = suggest_sites(q.stem + " " + " ".join(q.options.values()), self.sites)
        if hints:
            parts.append("\n根据题目，最可能用到的官方网站（优先直接 goto 这些网址，不要先用搜索引擎）：")
            parts += [f"- 模块{s['module']} {s['title']}：{' '.join(s['urls'][:6])}" for s in hints]
        if self._recipes:
            parts.append("\n以前答对过的相似题的做法（可以照着做，网址和步骤可直接复用）：")
            parts += [_clip(r, 700) for r in self._recipes]
        if steps:
            parts.append("\n已做的操作：")
            for i, s in enumerate(steps, 1):
                parts.append(f"{i}. {json.dumps(s.action, ensure_ascii=False)} → {_clip(s.result, 300)}")
        if self._memory:
            parts.append(f"\n你上一步记下的要点：{self._memory}")
        parts.append(f"\n当前页面：\n{observation}")
        if force:
            parts.append("\n时间到了：现在必须输出 answer 动作，给出最可能的答案。")
        else:
            parts.append(f"\n剩余时间约{remaining:.0f}秒。输出下一步的JSON。")
        return "\n".join(parts)

    async def _decide(self, q: Question, steps: list[Step], observation: str, remaining: float, force: bool) -> list[dict]:
        """The model's next actions (1-3); a single invalid marker on failure."""
        prompt = self._prompt(q, steps, observation, remaining, force)
        try:
            reply = await asyncio.to_thread(self.model.chat_json, self.system, prompt, 400)
        except LLMError as e:
            self.log(f"  (model: {str(e)[:80]})")
            if "JSON" in str(e):
                return [{"action": "_invalid", "error": "上一步输出不是JSON，请只输出一个JSON对象"}]
            # Timeouts and network hiccups: let the loop try again.
            return [{"action": "_invalid", "error": "模型请求失败，请直接给出下一步", "failed": True}]
        if not isinstance(reply, dict):
            return [{"action": "_invalid", "error": "输出必须是JSON对象"}]
        if reply.get("memory"):
            self._memory = str(reply["memory"])[:300]
        acts = reply.get("actions")
        if isinstance(acts, list):
            acts = [a for a in acts if isinstance(a, dict) and a.get("action")][:3]
        elif reply.get("action"):
            acts = [reply]  # a bare single action is fine too
        else:
            acts = []
        return acts or [{"action": "_invalid", "error": "没有给出动作，请输出 actions"}]

    def _load_recipes(self, q: Question) -> None:
        self._recipes = []
        if self.kb is None:
            return
        try:
            hits = self.kb.search(q.stem + " " + " ".join(q.options.values()), k=2, kind="recipe")
        except Exception:
            return
        self._recipes = [h.text for h in hits if h.score > 5]

    def _save_recipe(self, q: Question, steps: list[Step], final: dict, conf: float) -> None:
        """Remember how a confidently answered question was solved."""
        useful = [s for s in steps if s.action.get("action") not in ("（无效输出）",) and not s.result.startswith(("失败", "重复操作"))]
        if self.kb is None or conf < 0.7 or not useful:
            return
        lines = [f"题目：{q.stem}"]
        for i, s in enumerate(useful, 1):
            a = {k: v for k, v in s.action.items() if k != "ref"}
            lines.append(f"{i}. {json.dumps(a, ensure_ascii=False)}")
        if final.get("evidence"):
            lines.append(f"依据：{str(final['evidence'])[:200]}")
        source = "recipe:" + hashlib.sha1(q.stem.encode("utf-8")).hexdigest()[:16]
        try:
            self.kb.add_document(source, [(None, "\n".join(lines))], title=f"做法：{q.stem[:40]}", kind="recipe", module="recipe")
        except Exception:
            pass

    async def run(self, q: Question, budget: float = 75, max_steps: int = 15, close: bool = False) -> LiveResult:
        start = time.monotonic()
        self._memory = ""
        self._load_recipes(q)
        if self._recipes:
            self.log(f"  (found {len(self._recipes)} saved approach(es) for similar questions)")
        self.page = await self._open_tab()
        await self.page.bring_to_front()
        steps: list[Step] = []
        observation = "（空白页，还没有打开任何网站）"
        final: dict | None = None
        failures = 0
        try:
            for n in range(1, max_steps + 1):
                elapsed = time.monotonic() - start
                force = elapsed > budget - 6 or n == max_steps
                acts = await self._decide(q, steps, observation, budget - elapsed, force)
                if force and not any(a.get("action") == "answer" for a in acts):
                    # Out of time or steps: insist on an answer once.
                    acts = await self._decide(q, steps, observation, 0, True)
                if force:
                    final = next((a for a in acts if a.get("action") == "answer"), acts[0])
                    break
                if acts[0].get("action") == "_invalid":
                    failures = failures + 1 if acts[0].get("failed") else 0
                    if failures >= 3:
                        self.log("  (model unavailable - giving up on live research)")
                        break
                    steps.append(Step({"action": "（无效输出）"}, acts[0].get("error", "")))
                    continue
                failures = 0
                last_kind = None
                for j, action in enumerate(acts):
                    kind = action.get("action")
                    if kind == "answer":
                        final = action
                        break
                    label = f"[{n}{'abc'[j] if len(acts) > 1 else ''}]"
                    repeat = _repeats(action, steps)
                    if repeat:
                        self.log(f"  {label} (skipped repeat) {_describe(action)}")
                        steps.append(Step(action, f"重复操作，已跳过：{repeat}。换一个方法，比如打开目录中的官方网站、换关键词，或根据已有信息直接 answer。"))
                        break
                    self.log(f"  {label} {_describe(action)}")
                    try:
                        result = await asyncio.wait_for(self.act(action), timeout=30)
                    except Exception as e:
                        result = f"失败：{type(e).__name__}: {str(e).splitlines()[0][:150] if str(e) else ''}"
                    steps.append(Step(action, result))
                    last_kind = kind
                    if result.startswith("失败") or kind in PAGE_CHANGING or (kind == "type" and action.get("enter")):
                        break  # element numbers are stale now; look at the page again
                if final is not None:
                    break
                if last_kind in ("find", "pdf"):
                    observation = f"（仍在 {self.page.url}）\n{steps[-1].result}"
                else:
                    observation = await self.observe()
        finally:
            urls = [s.action.get("url") for s in steps if s.action.get("url")]
            if close and self.page is not None:
                try:
                    await self.page.close()
                except Exception:
                    pass

        final = final or {}
        answer = normalize_answer(q, final.get("answer"))
        try:
            conf = max(0.0, min(1.0, float(final.get("confidence", 0))))
        except (TypeError, ValueError):
            conf = 0.0
        if not answer:
            conf = 0.0
        reason = str(final.get("reason", ""))
        if final.get("evidence"):
            reason += f"  证据：{str(final['evidence'])[:200]}"
        if answer:
            self._save_recipe(q, steps, final, conf)
        ans = Answer(answer, conf, reason=reason, seconds=time.monotonic() - start)
        return LiveResult(ans, steps, urls)


PAGE_CHANGING = {"goto", "search", "click", "back"}


def _repeats(action: dict, steps: list[Step]) -> str:
    """A goto/search identical to an earlier one only loops; say which."""
    kind = action.get("action")
    key = {"goto": "url", "search": "query"}.get(kind)
    if not key:
        return ""
    value = str(action.get(key, "")).strip().rstrip("/")
    for s in steps:
        if s.action.get("action") == kind and str(s.action.get(key, "")).strip().rstrip("/") == value:
            return f"之前已经{'打开过这个网址' if kind == 'goto' else '搜索过同样的关键词'}"
    return ""


def _describe(a: dict) -> str:
    kind = a.get("action", "?")
    detail = {k: v for k, v in a.items() if k != "action"}
    return f"{kind} {json.dumps(detail, ensure_ascii=False)[:120]}"
