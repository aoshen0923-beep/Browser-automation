"""Recorded operation flows: show the tool a multi-step task once, replay it.

Some contest questions need long click sequences on complex sites (CNKI
advanced search, then 可视化分析, then 作者分布). Letting the model find its
way each time costs a model call per click. Instead you record the flow
once in the dedicated browser: every click, typed value and dropdown choice
is captured. Typed values become parameters. At answer time the model
picks a flow and fills its parameters in a single step, the flow replays in
seconds, and the model reads the resulting page.

Flows are stored in the knowledge base, so they travel with kb export/merge.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field

from playwright.async_api import BrowserContext, Page

# Injected into every frame of the recording tab. Reports user actions to
# Python through the exposed __qpRecord binding.
RECORDER_JS = r"""
(() => {
  if (window.__qpRecorder) return;
  window.__qpRecorder = true;
  const send = (ev) => { try { window.__qpRecord(ev); } catch (e) {} };
  const clean = (s) => (s || '').trim().replace(/\s+/g, ' ').slice(0, 60);
  const randomId = (id) => !id || /\d{4,}|[0-9a-f]{8,}|^(ember|react|mui|el-id|__)/i.test(id);
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      if (el.id && !randomId(el.id)) { parts.unshift('#' + CSS.escape(el.id)); break; }
      let part = el.tagName.toLowerCase();
      const parent = el.parentElement;
      if (parent) {
        const same = [...parent.children].filter((c) => c.tagName === el.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(el) + 1})`;
      }
      parts.unshift(part);
      el = parent;
    }
    return parts.join(' > ');
  };
  const labelOf = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.innerText);
    let prev = el.previousElementSibling;
    if (prev && clean(prev.innerText)) return clean(prev.innerText);
    const cell = el.closest('td,dd,li,div');
    if (cell && cell.previousElementSibling) return clean(cell.previousElementSibling.innerText);
    return '';
  };
  const describe = (el) => ({
    tag: el.tagName.toLowerCase(),
    id: randomId(el.id) ? '' : el.id,
    name: el.getAttribute('name') || '',
    placeholder: el.getAttribute('placeholder') || '',
    aria: el.getAttribute('aria-label') || '',
    title: el.getAttribute('title') || '',
    role: el.getAttribute('role') || '',
    type: (el.type || '').toLowerCase(),
    text: ['input', 'textarea', 'select'].includes(el.tagName.toLowerCase()) ? '' : clean(el.innerText || el.value),
    label: labelOf(el),
    css: cssPath(el),
  });
  const isTextField = (el) => el.matches('textarea, input:not([type]), input[type=text], input[type=search], ' +
                                         'input[type=number], input[type=email], input[type=tel], input[type=url]');
  document.addEventListener('click', (e) => {
    let el = e.target.closest('a,button,label,option,summary,[role=button],[role=link],[role=tab],[role=option],' +
                              '[role=menuitem],[onclick],li,td,span,div,input,select') || e.target;
    if (isTextField(el) || el.matches('select')) return;  // focusing a field isn't a step; its value is
    if (el.matches('input[type=checkbox], input[type=radio]')) return;  // recorded on change
    send({kind: 'click', target: describe(el), frame: location.href});
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.matches('select')) {
      const opt = el.options[el.selectedIndex];
      send({kind: 'select', target: describe(el), value: opt ? clean(opt.text) : '', frame: location.href});
    } else if (el.matches('input[type=checkbox], input[type=radio]')) {
      send({kind: 'check', target: describe(el), checked: el.checked, frame: location.href});
    } else if (isTextField(el)) {
      send({kind: 'fill', target: describe(el), value: el.value, frame: location.href});
    }
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && isTextField(e.target)) {
      send({kind: 'fill', target: describe(e.target), value: e.target.value, enter: true, frame: location.href});
    }
  }, true);
})();
"""


@dataclass
class Flow:
    name: str
    description: str
    start_url: str
    steps: list[dict]
    params: list[dict] = field(default_factory=list)  # [{"name", "example", "step"}]
    updated: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Flow":
        return cls(**json.loads(text))

    def summary(self) -> str:
        params = "、".join(f"{p['name']}（例：{p['example']}）" for p in self.params) or "无"
        return f"流程「{self.name}」：{self.description}。参数：{params}"


def _merge_steps(raw: list[dict]) -> list[dict]:
    """Collapse the event stream into replayable steps."""
    steps: list[dict] = []
    for ev in raw:
        kind = ev.get("kind")
        if kind == "fill" and steps and steps[-1]["kind"] == "fill" and steps[-1]["target"] == ev["target"]:
            # keydown(Enter) and change fire for the same field: keep one step
            steps[-1]["value"] = ev["value"]
            steps[-1]["enter"] = steps[-1].get("enter") or ev.get("enter", False)
            continue
        if kind == "fill" and not str(ev.get("value", "")).strip() and not ev.get("enter"):
            continue
        steps.append(dict(ev))
    return steps


def _param_name(target: dict, used: set[str]) -> str:
    base = target.get("label") or target.get("placeholder") or target.get("aria") or target.get("name") or "输入"
    base = re.sub(r"[:：*\s]+$", "", base)[:20] or "输入"
    name, i = base, 2
    while name in used:
        name, i = f"{base}{i}", i + 1
    used.add(name)
    return name


def build_flow(name: str, description: str, start_url: str, raw_events: list[dict]) -> Flow:
    steps = _merge_steps(raw_events)
    params, used = [], set()
    for i, st in enumerate(steps):
        if st["kind"] in ("fill", "select"):
            pname = _param_name(st["target"], used)
            st["param"] = pname
            params.append({"name": pname, "example": st.get("value", ""), "step": i})
    return Flow(name.strip() or "未命名流程", description.strip(), start_url, steps, params)


class Recorder:
    """Records what you do in one tab (and tabs it opens) of the dedicated browser."""

    def __init__(self, context: BrowserContext):
        self.context = context
        self.events: list[dict] = []
        self.page: Page | None = None
        self.start_url = ""
        self._pages: list[Page] = []

    async def _instrument(self, page: Page) -> None:
        async def on_event(source, ev):
            if isinstance(ev, dict):
                ev["page"] = self._pages.index(page) if page in self._pages else 0
                self.events.append(ev)

        self._pages.append(page)
        await page.expose_binding("__qpRecord", on_event)
        await page.add_init_script(RECORDER_JS)
        try:
            await page.evaluate(RECORDER_JS)
        except Exception:
            pass  # page still loading; the init script covers it

    async def start(self, url: str) -> Page:
        self.start_url = url
        self.page = await self.context.new_page()
        await self._instrument(self.page)
        self.context.on("page", self._on_new_page)
        await self.page.bring_to_front()
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return self.page

    def _on_new_page(self, page: Page) -> None:
        # Tabs opened by the recorded page (e.g. 可视化分析 opens a new window).
        if page.opener is not None:
            asyncio.ensure_future(self._instrument(page))

    def stop(self) -> list[dict]:
        try:
            self.context.remove_listener("page", self._on_new_page)
        except Exception:
            pass
        return list(self.events)


# --- replay ---------------------------------------------------------------------


def _candidates(frame, t: dict):
    """Ways to find a recorded element again, most specific first."""
    tag = t.get("tag") or "*"
    out = []
    if t.get("id"):
        out.append(frame.locator(f"#{_css_escape(t['id'])}"))
    if t.get("name") and tag in ("input", "select", "textarea", "button"):
        out.append(frame.locator(f'{tag}[name="{t["name"]}"]'))
    if t.get("placeholder"):
        out.append(frame.get_by_placeholder(t["placeholder"], exact=True))
    if t.get("aria"):
        out.append(frame.locator(f'[aria-label="{t["aria"]}"]'))
    if t.get("text") and tag not in ("input", "select", "textarea"):
        out.append(frame.locator(tag).filter(has_text=re.compile("^\\s*" + re.escape(t["text"]) + "\\s*$")))
        out.append(frame.get_by_text(t["text"], exact=True))
    if t.get("title"):
        out.append(frame.locator(f'[title="{t["title"]}"]'))
    if t.get("css"):
        out.append(frame.locator(t["css"]))
    return out


def _css_escape(s: str) -> str:
    return re.sub(r"([^\w-])", r"\\\1", s)


async def _find(page: Page, step: dict, timeout: float = 10.0):
    """First visible match for the step's target, waiting for slow pages."""
    frame_url = step.get("frame", "")
    deadline = time.monotonic() + timeout
    while True:
        frames = [page.main_frame]
        if frame_url and frame_url.split("?")[0] != page.url.split("?")[0]:
            same = [f for f in page.frames if f.url.split("?")[0] == frame_url.split("?")[0]]
            frames = same + frames
        for frame in frames:
            for loc in _candidates(frame, step["target"]):
                try:
                    if await loc.count() and await loc.first.is_visible():
                        return loc.first
                except Exception:
                    continue
        if time.monotonic() > deadline:
            t = step["target"]
            raise LookupError(f"找不到录制时的元素：{t.get('label') or t.get('text') or t.get('placeholder') or t.get('css')}")
        await asyncio.sleep(0.4)


async def _settle(page: Page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=2500)
    except Exception:
        pass


async def replay(context: BrowserContext, page: Page, flow: Flow, params: dict | None = None, log=print) -> Page:
    """Run a recorded flow with new parameter values; return the page it ends on."""
    params = {str(k): str(v) for k, v in (params or {}).items()}
    await page.goto(flow.start_url, wait_until="domcontentloaded", timeout=30000)
    await _settle(page)
    for i, step in enumerate(flow.steps, 1):
        kind = step["kind"]
        loc = await _find(page, step)
        before = len(context.pages)
        if kind == "click":
            try:
                await loc.click(timeout=5000)
            except Exception:
                await loc.evaluate("e => e.click()")
        elif kind == "fill":
            value = params.get(step.get("param", ""), step.get("value", ""))
            await loc.fill(value, timeout=5000)
            if step.get("enter"):
                await loc.press("Enter")
        elif kind == "select":
            value = params.get(step.get("param", ""), step.get("value", ""))
            await loc.select_option(label=value, timeout=5000)
        elif kind == "check":
            await loc.set_checked(bool(step.get("checked")), timeout=5000)
        await asyncio.sleep(0.3)
        if len(context.pages) > before:  # the step opened a new tab: follow it
            page = context.pages[-1]
            await page.bring_to_front()
        await _settle(page)
        log(f"    flow step {i}/{len(flow.steps)}: {kind} ok")
    return page


def describe_step(step: dict) -> str:
    t = step.get("target", {})
    what = t.get("label") or t.get("text") or t.get("placeholder") or t.get("aria") or t.get("name") or t.get("tag", "")
    kind = step.get("kind")
    if kind == "fill":
        return f"输入「{what}」= {step.get('value', '')}" + ("（回车）" if step.get("enter") else "")
    if kind == "select":
        return f"选择「{what}」= {step.get('value', '')}"
    if kind == "check":
        return f"{'勾选' if step.get('checked') else '取消勾选'}「{what}」"
    return f"点击「{what}」"
