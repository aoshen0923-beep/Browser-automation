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
from urllib.parse import quote_plus, urljoin, urlparse

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from . import pdftools
from .browser import BLOCKED_RESOURCES, Browser, goto, page_blocked, wait_for_human
from .kb import KB
from .llm import ChatModel, LLMError, VisionUnsupported
from . import memory
from .pointer import Pointer
from .question import JUDGE, MULTI, SINGLE, Question
from .solver import Answer, normalize_answer

# Renders the visible page as text in reading order, with each interactive
# element marked in place: "GB/T 38880-2020 儿童口罩 [12]<a>查看全文</a>". The
# model sees which link belongs to which result. Elements keep their number
# (data-qp) across observations, so a number read earlier still clicks the
# same element. Dialogs come first; long menus are moved to the end.
RENDER_JS = r"""
({prefix, navCap}) => {
  const MAX = 200000;
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','IFRAME','FRAME','OBJECT','EMBED','CANVAS','SVG','svg','HEAD','LINK','META']);
  const BLOCK = new Set(['ADDRESS','ARTICLE','ASIDE','BLOCKQUOTE','BR','CAPTION','DD','DETAILS','DIALOG','DIV','DL','DT','FIELDSET',
    'FIGCAPTION','FIGURE','FOOTER','FORM','H1','H2','H3','H4','H5','H6','HEADER','HR','LI','MAIN','NAV','OL','P','PRE','SECTION',
    'TABLE','TBODY','TFOOT','THEAD','TR','UL','LEGEND']);
  const INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=option],[role=checkbox],[role=radio],[role=switch],[role=treeitem],[onclick],[contenteditable=""],[contenteditable=true]';
  const NAV = 'nav,header,footer,[role=navigation],[role=banner],[role=contentinfo],[class*=nav i],[id*=nav i],' +
    '[class*=menu i],[id*=menu i],[class*=header i],[id*=header i],[class*=footer i],[id*=footer i]';
  const DIALOG = 'dialog[open],[role=dialog],[role=alertdialog],[aria-modal=true],.modal.show,.modal.in,.layui-layer,' +
    '.el-dialog__wrapper,.el-message-box__wrapper,.ant-modal-wrap';
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  const ptr = el => !!el && el !== document.body && el !== document.documentElement && getComputedStyle(el).cursor === 'pointer';
  const shown = el => {
    if (el.checkVisibility) return el.checkVisibility({checkVisibilityCSS: true, visibilityProperty: true});
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && el.getClientRects().length > 0;
  };
  let next = window.__qpNext || 1;
  const used = new Set();
  const refOf = el => {
    let r = el.getAttribute('data-qp');
    const mine = r && (prefix ? r.startsWith(prefix) : !r.includes('-'));
    if (!mine || used.has(r)) { r = prefix + (next++); el.setAttribute('data-qp', r); }
    used.add(r);
    return r;
  };
  const labelOf = el => {
    const img = el.querySelector && el.querySelector('img[alt],img[title]');
    return clean(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') ||
                 el.getAttribute('alt') || (img && (img.alt || img.title)) || el.getAttribute('placeholder') || '').slice(0, 80);
  };
  const state = el => {
    let s = (el.getAttribute('aria-selected') === 'true' || el.getAttribute('aria-checked') === 'true' ||
      el.getAttribute('aria-current') && el.getAttribute('aria-current') !== 'false' ||
      /(^|\s)(active|current|cur|on|selected)(\s|$)/i.test(typeof el.className === 'string' ? el.className : '')) ? ' 当前' : '';
    // Folded filter panels / menus: the options inside only show after a click.
    const folded = el.getAttribute('aria-expanded') === 'false' ||
      (el.tagName === 'SUMMARY' && el.parentElement && !el.parentElement.open);
    if (folded) s += ' 已折叠-点开才能看到里面的选项';
    else if (el.getAttribute('aria-expanded') === 'true') s += ' 已展开';
    return s;
  };
  const atom = el => {
    const tag = el.tagName.toLowerCase();
    const ref = refOf(el);
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return `[${ref}]<input type=${t}${el.checked ? ' checked' : ''}>`;
      if (['button', 'submit', 'reset', 'image'].includes(t)) return `[${ref}]<button>${clean(el.value || el.alt || el.title || t)}</button>`;
      let s = `[${ref}]<input type=${t}`;
      const ph = clean(el.placeholder || el.getAttribute('aria-label') || el.title || '');
      if (ph) s += ` placeholder="${ph.slice(0, 40)}"`;
      if (el.value) s += ` value="${clean(el.value).slice(0, 60)}"`;
      return s + '>';
    }
    if (tag === 'textarea') return `[${ref}]<textarea${el.value ? ` value="${clean(el.value).slice(0, 60)}"` : ''}>`;
    if (tag === 'select') {
      const opts = [...el.options].map(o => clean(o.text));
      const sel = el.selectedOptions && el.selectedOptions[0];
      return `[${ref}]<select 已选="${sel ? clean(sel.text) : ''}" 选项=${opts.slice(0, 40).join('|')}${opts.length > 40 ? '|…' : ''}>`;
    }
    const role = el.getAttribute('role');
    const name = tag === 'a' || role === 'link' ? 'a' : tag === 'button' || role === 'button' ? 'button' : (role || tag);
    let label = labelOf(el);
    const href = tag === 'a' ? (el.getAttribute('href') || '') : '';
    const file = /\.(pdf|docx?|xlsx?|pptx?|caj|zip|rar)([?#]|$)/i.test(href);
    if (!label && href && !href.startsWith('javascript')) label = href.split('?')[0].split('/').filter(Boolean).pop() || href;
    return `[${ref}]<${name}${file ? ' href=' + href.slice(0, 90) : ''}${state(el)}>${label || '(无文字)'}</${name}>`;
  };

  const out = [];
  let size = 0;
  const extra = [];
  const push = s => { if (size < MAX) { out.push(s); size += s.length; } };
  const box = window.__qpScrollBox && window.__qpScrollBox.isConnected ? window.__qpScrollBox : null;
  const vpTop = box ? box.getBoundingClientRect().top : 0;
  let marked = false;
  const mark = el => {
    if (marked || !el || (box && !box.contains(el))) return;
    const r = el.getBoundingClientRect();
    if (r.bottom > vpTop + 1 && r.height > 0) { marked = true; push('\u0001'); }
  };
  const skipDialogs = new Set();
  let nav = null;  // {count, hidden} while inside a menu/header/footer region

  const walk = (node, inAtomParent) => {
    if (size >= MAX) return;
    if (node.nodeType === 3) {
      const t = node.textContent.replace(/\s+/g, ' ');
      if (t.trim()) { mark(node.parentElement); push(t); }
      return;
    }
    if (node.nodeType !== 1) return;
    const el = node;
    if (el.id === '__qp_pointer' || skipDialogs.has(el)) return;
    // Text-less badges and icons ("Open Access", "PDF", lock icons) still carry meaning.
    if (!el.textContent.trim() && !el.matches(INTERACTIVE) && el.tagName !== 'IMG' && shown(el)) {
      const t = el.tagName === 'svg' || el.tagName === 'SVG' ? el.querySelector('title') : null;
      const cls = typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '');
      let hint = clean(el.getAttribute('aria-label') || el.getAttribute('title') || (t && t.textContent) ||
                       el.getAttribute('data-title') || el.getAttribute('data-tooltip') || '');
      if (!hint && /open.?access|lock.?open|unlock|(^|[\s_-])oa([\s_-]|$)/i.test(cls)) hint = 'Open Access';
      if (!hint && /(^|[\s_-])(free|gratis)([\s_-]|$)/i.test(cls)) hint = 'Free';
      if (hint) { push(` [图标:${hint.slice(0, 40)}] `); return; }
    }
    if (SKIP.has(el.tagName)) return;
    if (!shown(el)) return;
    const block = BLOCK.has(el.tagName);
    const hard = el.matches(INTERACTIVE) || typeof el.onclick === 'function';
    // Script-driven widgets (custom dropdowns, tabs): the outermost element with a hand cursor.
    if (!hard && ptr(el) && !ptr(el.parentElement)) {
      const ref = refOf(el);
      mark(el);
      if (!clean(el.innerText)) { push(` [${ref}]<button>${labelOf(el) || '(图标)'}</button> `); return; }
      push(`${block ? '\n' : ' '}[${ref}]<button${state(el)}>`);
      for (const c of el.childNodes) walk(c);
      if (el.shadowRoot) for (const c of el.shadowRoot.childNodes) walk(c);
      push(`</button>${block ? '\n' : ' '}`);
      return;
    }
    if (hard) {
      const tag = el.tagName;
      const r = el.getBoundingClientRect();
      const tiny = r.width < 1 && r.height < 1 && tag !== 'INPUT';
      const container = !['INPUT', 'SELECT', 'TEXTAREA'].includes(tag) &&
        (el.querySelector(INTERACTIVE) || clean(el.innerText).length > 80);
      if (!tiny && !container) {
        mark(el);
        const s = atom(el);
        if (nav && ++nav.count > navCap) { nav.hidden++; extra.push(s); }
        else push((block ? '\n' : ' ') + s + (block ? '\n' : ' '));
        return;
      }
      if (container) {
        const ref = refOf(el);
        mark(el);
        push(`${block ? '\n' : ' '}[${ref}]<${tag === 'A' ? 'a' : 'button'}${state(el)}>`);
        for (const c of el.childNodes) walk(c);
        if (el.shadowRoot) for (const c of el.shadowRoot.childNodes) walk(c);
        push(`</${tag === 'A' ? 'a' : 'button'}>${block ? '\n' : ' '}`);
        return;
      }
    }
    let entered = false;
    if (!nav && el.matches(NAV) && el !== document.body && el !== document.documentElement) {
      nav = {count: 0, hidden: 0};
      entered = true;
    }
    if (block) push('\n');
    if (el.tagName === 'IMG' && el.alt) push(` [图:${clean(el.alt).slice(0, 40)}] `);
    for (const c of el.childNodes) walk(c);
    if (el.shadowRoot) for (const c of el.shadowRoot.childNodes) walk(c);
    if (el.tagName === 'TD' || el.tagName === 'TH') push(' | ');
    if (block) push('\n');
    if (entered) {
      if (nav.hidden) push(` （…另有${nav.hidden}个菜单/导航链接，放在页面文字末尾）\n`);
      nav = null;
    }
  };

  const root = document.body || document.documentElement;
  const dialogs = [...document.querySelectorAll(DIALOG)].filter(d => shown(d) && d.getBoundingClientRect().height > 20);
  if (dialogs.length) {
    push('【页面上弹出的对话框】\n');
    for (const d of dialogs) { if (![...skipDialogs].some(x => x.contains(d))) { walk(d); skipDialogs.add(d); } }
    push('\n【对话框结束，下面是页面】\n');
  }
  if (root) walk(root);
  window.__qpNext = next;
  if (extra.length) push('\n【菜单/导航链接】 ' + extra.join(' '));
  let text = out.join('').replace(/[ \t ]+/g, ' ').replace(/ ?\n ?/g, '\n').replace(/\n{2,}/g, '\n').replace(/( \| ){2,}/g, ' | ');
  let vp = text.indexOf('\u0001');
  text = text.replace('\u0001', '');
  const se = document.scrollingElement || document.documentElement;
  const sb = box || se;
  return {title: document.title, url: location.href, text: text.trim(), vp: Math.max(0, vp),
          scroll: {y: sb.scrollTop, h: sb.scrollHeight, v: box ? box.clientHeight : innerHeight}};
}
"""

SIGNATURE_JS = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  let h = 0;
  for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) | 0;
  return [location.href, t.length, h, document.querySelectorAll(':checked').length,
          document.querySelectorAll('[aria-expanded=true],[aria-selected=true]').length].join('|');
}
"""

# [text length, whether a "loading…" indicator is showing]
LOADING_JS = r"""
() => {
  const body = document.body || document.documentElement;
  const vis = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const w = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let n, i = 0, loading = false;
  while ((n = w.nextNode()) && i++ < 20000) {
    const s = n.textContent.trim();
    if (s.length < 20 && /^(正在加载|加载中|数据加载中|正在检索|检索中|正在查询|请稍候|请稍等|loading)[\s.。…]*$/i.test(s) && vis(n.parentElement)) {
      loading = true;
      break;
    }
  }
  if (!loading) loading = [...document.querySelectorAll('.el-loading-mask,.layui-layer-loading,.ant-spin-spinning,.loading-mask')].some(vis);
  return [(document.body ? document.body.innerText : '').length, loading];
}
"""

# Download a file from inside the page (base64), using the page's own cookies.
FETCH_JS = r"""
async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  const b = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return btoa(s);
}
"""

SCROLL_JS = r"""
(dir) => {
  const se = document.scrollingElement || document.documentElement;
  const can = el => el.scrollHeight > el.clientHeight + 20;
  let target = se;
  if (!can(se)) {
    let best = null, area = 0;
    for (const el of document.querySelectorAll('body *')) {
      if (!can(el)) continue;
      const oy = getComputedStyle(el).overflowY;
      if (oy !== 'auto' && oy !== 'scroll') continue;
      const r = el.getBoundingClientRect();
      if (r.width * r.height > area) { area = r.width * r.height; best = el; }
    }
    if (best) target = best;
  }
  window.__qpScrollBox = target === se ? null : target;
  const view = target === se ? innerHeight : target.clientHeight;
  const before = target.scrollTop;
  if (dir === 'top') target.scrollTop = 0;
  else if (dir === 'bottom') target.scrollTop = target.scrollHeight;
  else target.scrollTop += (dir === 'up' ? -0.85 : 0.85) * view;
  return {moved: Math.round(target.scrollTop - before), y: target.scrollTop, h: target.scrollHeight, v: view};
}
"""

# Finds the "next page" control of a result list and tags it for a real click.
NEXT_PAGE_JS = r"""
() => {
  document.querySelectorAll('[data-qp-next]').forEach(e => e.removeAttribute('data-qp-next'));
  const exact = /^(下一页|下页|后一页|下一頁|next|nextpage|›|»|>|>>|→)$/i;
  const cands = [...document.querySelectorAll('a,button,[role=button],[onclick],input[type=button],input[type=submit],li,span,i')]
    .filter(el => {
      const t = (el.innerText || el.value || '').replace(/\s+/g, '');
      const hint = (el.getAttribute('aria-label') || '') + (el.getAttribute('title') || '') + (el.className && el.className.baseVal === undefined ? el.className : '');
      const hit = exact.test(t) || /下一页|next/i.test(hint) && t.length < 6;
      if (!hit || !el.getClientRects().length) return false;
      // Bare arrows (">") are often just separators: only count them when they look clickable.
      if (!/[\u4e00-\u9fa5a-z]/i.test(t) && ['LI', 'SPAN', 'I'].includes(el.tagName) && !el.closest('a,button,[onclick]') &&
          getComputedStyle(el).cursor !== 'pointer' && !/disabled/i.test(el.className || '')) return false;
      return true;
    });
  const inner = cands.filter(el => !cands.some(o => o !== el && el.contains(o)));
  const enabled = inner.filter(el => {
    const c = el.closest('a,button,li,[onclick]') || el;
    return !el.disabled && c.getAttribute('aria-disabled') !== 'true' && !/disabled/i.test((c.className || '') + (el.className || ''));
  });
  if (!enabled.length) return inner.length ? 'last' : 'none';
  enabled[enabled.length - 1].setAttribute('data-qp-next', '1');
  return 'ok';
}
"""


# How to use the big databases quickly (URL patterns, where the answer-bearing labels are).
SITE_TIPS = {
    "dl.acm.org": "检索直接用 https://dl.acm.org/action/doSearch?AllField=论文标题 ；结果和文章页上的 OPEN ACCESS"
                  " 标记（可能显示为 [图标:…]）表示OA；文章页的 Pages 1 - N 就是页数；全文PDF是"
                  " https://dl.acm.org/doi/pdf/DOI ，用 pdf 动作读（page=2 可看第2页的图注）；要核对几篇论文时用 open_many 同时检索每个标题",
    "cell.com": "Cell官网：首页右上角 Search 是 Quick Search（可选检索字段），旁边有 Advanced Search（有 Access Filter 等筛选项，"
                "要点开看有几个选项）；过刊在 https://www.cell.com/cell/archive 按年份/卷/期浏览，每期目录按文献类型分组"
                "（Articles 等），数篇数要打开那一期逐组数；文章页写着页码范围（如 p1777–1792.e21）和 PDF 链接",
    "science.org": "检索直接用 https://www.science.org/action/doSearch?AllField=关键词 ；过刊目录 https://www.science.org/loi/science ，"
                   "某卷某期 https://www.science.org/toc/science/卷/期 ；文章页有 PDF 链接",
    "sciencedirect.com": "检索直接用 https://www.sciencedirect.com/search?qs=关键词 ；Open access 文章有标记；期刊页可按卷期浏览",
    "wanfangdata.com.cn": "万方检索直接用 https://s.wanfangdata.com.cn/paper?q=关键词 （期刊论文 /periodical?q= ，学位论文 /thesis?q= ，"
                          "会议论文 /conference?q= ）；结果页左侧可按年份、资源类型等筛选，检索式可在高级检索里写",
    "cqvip.com": "维普：用首页或期刊检索页的检索框（可选题名、关键词、作者等字段），高级检索里可组合条件；结果页有被引、下载等信息",
    "tandfonline.com": "Taylor & Francis 检索直接用 https://www.tandfonline.com/action/doSearch?AllField=关键词 ；"
                       "Open access 文章有标记；文章页有 PDF 链接",
    "cnki.net": "知网：高级检索在 https://kns.cnki.net/kns8s/AdvSearch （主题/篇名/作者等字段是下拉框，可用 click 文字选择）；"
                "复杂检索优先用录制好的流程；结果页有被引、下载次数，详情页有基金、分类号等",
    "nature.com": "检索直接用 https://www.nature.com/search?q=关键词&journal=nature （去掉 journal 参数搜全部 Nature 期刊）；"
                  "文章页有 Open access 标记；PDF 通常是文章网址加 .pdf",
    "onlinelibrary.wiley.com": "Wiley 检索直接用 https://onlinelibrary.wiley.com/action/doSearch?AllField=关键词 ；Open Access 有标记",
    "asmedigitalcollection.asme.org": "ASME 检索直接用 https://asmedigitalcollection.asme.org/search-results?q=关键词 ；"
                                      "结果可按期刊/会议论文集筛选",
    "ieeexplore.ieee.org": "检索直接用 https://ieeexplore.ieee.org/search/searchresult.jsp?queryText=关键词 ；"
                           "会议论文集在结果左侧 Conferences 筛选；会议信息也可查 https://conferences.ieee.org",
    "conf.cnki.net": "CNKI 中国学术会议网：会议预告、会议信息检索，用页面上的检索框按会议名称/主办单位查",
    "nlc.cn": "国家图书馆：馆藏目录检索 http://opac.nlc.cn ；博士论文在国图的学位论文资源里检索，详情页有学位授予单位、年份等",
    "dspace.mit.edu": "MIT Theses：检索直接用 https://dspace.mit.edu/discover?query=关键词 ，可按院系、年份、学位筛选",
    "doaj.org": "DOAJ：首页检索框可选 Journals（期刊）或 Articles（文章）；期刊详情有 APC、许可协议、出版方等",
    "arxiv.org": "检索直接用 https://arxiv.org/search/?query=关键词&searchtype=all ；摘要页 https://arxiv.org/abs/编号 ，"
                 "PDF https://arxiv.org/pdf/编号 （用 pdf 动作读），摘要页有提交历史 v1/v2",
    "oalib.com": "OALIB：首页检索框检索，结果为开放获取论文，可直接下载 PDF",
    "chinaxiv.org": "ChinaXiv：首页检索框检索中文预印本，详情页有版本、学科分类、提交时间",
    "medrxiv.org": "检索直接用 https://www.medrxiv.org/search/关键词 ；详情页有版本、发布日期和 PDF",
    "biorxiv.org": "检索直接用 https://www.biorxiv.org/search/关键词 ；详情页有版本、发布日期和 PDF",
    "pubmed.ncbi.nlm.nih.gov": "检索直接用 https://pubmed.ncbi.nlm.nih.gov/?term=关键词",
    "link.springer.com": "检索直接用 https://link.springer.com/search?query=关键词 ；Open access 文章有标记",
}

# Names in a question → the site whose tips apply ("science" is checked after ScienceDirect).
TIP_NAMES = [
    (r"acm", "dl.acm.org"), (r"(?<![a-z])cell(?![a-z])", "cell.com"), (r"sciencedirect|elsevier", "sciencedirect.com"),
    (r"(?<![a-z])science(?![a-z])(?!\s*direct)", "science.org"), (r"万方|wanfang", "wanfangdata.com.cn"),
    (r"维普|cqvip", "cqvip.com"), (r"taylor|tandf", "tandfonline.com"), (r"知网|cnki", "cnki.net"),
    (r"学术会议网", "conf.cnki.net"), (r"(?<![a-z])nature(?![a-z])", "nature.com"), (r"wiley", "onlinelibrary.wiley.com"),
    (r"asme", "asmedigitalcollection.asme.org"), (r"ieee", "ieeexplore.ieee.org"), (r"国家图书馆|国图", "nlc.cn"),
    (r"(?<![a-z])mit(?![a-z])", "dspace.mit.edu"), (r"doaj", "doaj.org"), (r"(?<!china)arxiv", "arxiv.org"),
    (r"oalib", "oalib.com"), (r"chinaxiv", "chinaxiv.org"), (r"medrxiv", "medrxiv.org"), (r"biorxiv", "biorxiv.org"),
    (r"pubmed", "pubmed.ncbi.nlm.nih.gov"), (r"springer", "link.springer.com"),
]


def tips_for_question(text: str) -> list[str]:
    low = text.lower()
    hosts = dict.fromkeys(h for pat, h in TIP_NAMES if re.search(pat, low))
    return [f"{h}：{SITE_TIPS[h]}" for h in hosts]


def site_tips(url: str) -> str:
    host = memory._host(url)
    return next((tip for h, tip in SITE_TIPS.items() if host == h or host.endswith("." + h)), "")


LOGIN_WALL = re.compile(r"请先登录|登录后(?:查看|下载|阅读|可)|需要登录|您还没有登录|Sign in to (?:view|access|read|download)|"
                        r"Log in to (?:view|access|read)|Purchase (?:this )?(?:article|access)|Get access|Access through your institution|"
                        r"Subscribe to (?:read|access)", re.I)


def _mark(status: str) -> str:
    status = status.strip()
    if status.startswith(("对", "正确", "是", "符合")):
        return "✓"
    if status.startswith(("错", "不对", "不正确", "否", "不符合")):
        return "✗"
    return "?"


def _plain(text: str) -> str:
    """Rendered page text without the element markers (for the knowledge base)."""
    text = re.sub(r"\[\d+(?:-\d+)?\]", "", text)
    return re.sub(r"</?[a-z]+(?: [^<>]*)?>", " ", text)


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def evidence_found(evidence: str, seen: str) -> bool:
    """Does the quoted evidence really appear in the text that was read?

    Tolerates the model stitching quotes together with …/，and small
    wording slips in long quotes; a quote that isn't there at all fails.
    """
    corpus = _norm(seen)
    frags = [f for f in (_norm(x) for x in re.split(r"…+|\.{2,}|[。；;，,\n]", evidence)) if len(f) >= 4]
    if not frags:
        return True
    hits = 0
    for f in frags:
        if f in corpus:
            hits += 1
        elif len(f) > 16:
            shingles = [f[i:i + 8] for i in range(0, len(f) - 7, 4)]
            if sum(sh in corpus for sh in shingles) >= 0.6 * len(shingles):
                hits += 1
    return hits * 2 >= len(frags)


ACTIONS = """每一步输出一个JSON对象：
{"memory":"一两句话：已经确认了什么、下一步打算",
 "options":{"A":"对：在哪看到的依据","B":"错：依据","C":"待查：打算怎么查","D":"待查"},
 "actions":[动作1, 动作2, ...]}
options 是每个选项的核实情况（判断题只写一项 "题干"），每步都更新：只有在页面上亲眼看到依据的才写 对/错，其余写 待查。
actions 最多3个，按顺序执行；会改变页面的动作（goto/search/click/back/带回车的type）之后的动作不再执行，
所以把"输入+点击检索"这类组合放在同一步里能省时间。可用动作：
{"action":"goto","url":"https://..."}                 打开网址（优先用下面目录里的官方网站）
{"action":"search","query":"...","engine":"bing"}     用搜索引擎搜索（engine 可选 bing / baidu）
{"action":"click","ref":12}                          点击页面文字里标成 [12]<a>…</a> 的元素（内嵌框架里的编号形如 "2-5"）
{"action":"type","ref":5,"text":"...","enter":true}  在输入框5输入文字，enter=true 表示输入后按回车
{"action":"select","ref":7,"option":"学位论文"}         在下拉框7中选择一个选项
{"action":"click","text":"篇名"}                      按文字点击（菜单项等没有编号的地方）
{"action":"hover","ref":12}                          鼠标移到元素上（打开要悬停才出现的菜单）
{"action":"scroll","direction":"down"}               上下滑动页面：down/up 一屏，top/bottom 到顶/到底；加 "ref":12 滚到那个元素
{"action":"next_page"}                               结果列表翻到下一页（自动找"下一页"按钮；也可以直接点页码）
{"action":"find","text":"起草人|起草单位"}             在当前页面全文中查找关键词（多个关键词用 | 分隔）
{"action":"open_many","urls":["https://...","https://..."],"find":"Open Access|Pages"}
                                                     同时打开最多5个网址（后台标签页一起加载），返回每页里关键词所在的行和开头内容；
                                                     多选题要逐个核对选项时用（比如每个选项一个检索网址），比一个个打开快得多
{"action":"count","text":"PDF","from":"Articles","to":"Resources"}
                                                     数数：在整页（可限定从标题"from"到"to"之间）数含"text"的行，并列出来；
                                                     "某期有几篇""有几个选项""结果有多少条"这类题一定用它，不要自己数
{"action":"read","from":5000}                        读取当前页面第5000字之后的内容（带元素编号，页面文字被截断时用）
{"action":"pdf","url":"(可省略=当前页)","page":2,"find":"关键词"}  读取PDF：页数、指定页（负数从末尾算，-2=倒数第二页）、或查找关键词
{"action":"back"}                                    返回上一页
{"action":"look","question":"柱状图里排第一的作者是谁？"}  看当前页面截图回答问题（图表、只有图标没有文字的按钮、页面布局）；要点击图标时问它的位置
{"action":"click_xy","x":420,"y":310}                按截图上的坐标点击（配合 look 找到的位置）
读PDF时加 "look":"这一页有几张图" 可以直接看那一页的图片
{"action":"answer","answer":"C","confidence":0.9,"reason":"一句话","evidence":"页面上看到的原文"}
answer 的依据必须来自页面上看到的内容；没看到的不要编造，用较低的 confidence 表示。"""

SYSTEM_TEMPLATE = """你在操作用户的Chrome浏览器，为信息素养大赛查找客观题的答案。像熟练的真人一样高效操作。
规则：
1. 优先直接打开下面目录中对应模块的官方网站，不要先用搜索引擎；目录里没有的网站再用搜索。
2. 很多网站可以直接在URL里带检索词；知道就直接用，省去点击。
3. 找到能判断答案的原文后立刻 answer，不要多余操作。每个选项都要核对。
4. 不要重复同一个失败的动作；元素编号每一步都会刷新，只用最新的编号。
5. 页面需要登录或出现验证码时，程序会暂停等用户处理，你继续即可。
6. 题库、答案分享、问答类网站（如 itihey、百度知道、百度文库、作业帮、道客巴巴）上的答案经常是错的，
   只能当线索，要到官方网站核实；只凭这类网站作答时 confidence 不要超过0.5。
7. 多选题四个选项都要各自找到依据，options 里还有"待查"就继续查，全部核实完再 answer（时间到了除外）；
   判断题/单选题也要找到原文。常见题型：论文页数看文章页的 Pages 或 pdf 的"共N页"；"第N页有几张图/表"用 pdf page=N 看图注个数，
   没把握再加 look 看那一页；判断是否 OA 看检索结果/文章页的 Open Access 标记，多个选项要逐个核对，别凭印象。
   浏览器里打开的 PDF 你翻不了页，一律用 pdf 动作读。
   要分别核对几个选项/几篇论文时，用 open_many 一次同时打开（每个一个检索网址），不要一个个来。
   open_many 只返回关键词附近的几行；选项要在网站里操作才能核对的（检索界面有哪些字段、筛选项、过刊目录、某期有几篇），
   直接 goto 那个页面去点、去看，不要反复 open_many。
   答案常在页面后半部分（详情、表格、全文、名单）：页面文字被截断时先用 find 查题目关键词或用 read 读后面的内容，
   看全了再 answer，不要只看开头就下结论；每个选项都要在页面上找到依据。
8. 页面文字里 [编号]<a>文字</a> 标出了可以点的元素，就在它在页面上的位置，根据周围文字判断点哪个
   （比如某条结果那一行后面的「下载」「详情」）；编号在同一页面上一直有效。点击后程序会告诉你页面有没有变化，
   没变化说明没点对，换一个元素或方法；结果多时用 next_page 翻页，或先 find 关键词。
9. 页面还在加载时程序会多等一会；用户也可能暂停你、自己操作浏览器后让你继续，这时当前页面就是用户找到的页面，先仔细读它。
10. answer 的 evidence 要逐字抄页面原文，程序会核对原文是否真的在看过的页面里。
11. answer 不能为空：单选一个字母，多选2-4个字母，判断题"对"或"错"。时间不够时也要给出最可能的答案，用 confidence（0-1，诚实）表示把握。
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


class Control:
    """The buttons on the answering page: 我来操作 (pause), 继续作答, 停止.

    Methods are called on the event loop (the web server uses
    loop.call_soon_threadsafe). `changed` wakes up whatever the agent is
    waiting on (a model call, a slow page) so a button acts at once.
    """

    def __init__(self):
        self.stop = False
        self._resume = asyncio.Event()
        self._resume.set()
        self.changed = asyncio.Event()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def pause(self) -> None:
        if not self.stop:
            self._resume.clear()
            self.changed.set()

    def resume(self) -> None:
        self._resume.set()

    def request_stop(self) -> None:
        self.stop = True
        self._resume.set()
        self.changed.set()

    async def wait_resumed(self) -> None:
        await self._resume.wait()


_INTERRUPTED = object()


class Agent:
    def __init__(
        self,
        browser: Browser,
        model: ChatModel,
        sites: list[dict],
        kb: KB | None = None,
        *,
        max_items: int = 70,
        max_text: int = 5000,
        log=print,
        logged_in: list[dict] | None = None,
        vision: bool = True,
        control: Control | None = None,
        pointer: bool = True,
    ):
        self.browser = browser
        self.model = model
        self.kb = kb
        self.sites = sites
        # A model that rejected images once stays text-only for the session.
        self.vision = vision and not getattr(model, "vision_unsupported", False)
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
        self._lessons: list[str] = []
        self._known = ""
        self._noted_hosts: set[str] = set()
        self._flows: list = []
        self._saved: set[str] = set()
        self._prepared: set = set()
        self._rendered = ""
        self._downloads: list[str] = []
        self._kept = None
        self._checks: dict[str, str] = {}
        self.opened: list[Page] = []  # tabs this agent opened (the answering page tidies old ones)  # option letter -> "对：依据" / "错：…" / "待查"
        self.trace: list[dict] = []  # everything the model saw and did, for the run record
        self._window_end = 0
        self._seen: list[str] = []
        self._checked_evidence = False
        self._steps: list[Step] = []
        self._paused_for = 0.0
        self._previous = ""
        self.control = control
        self.pointer = Pointer(pointer)

    # --- page handling -------------------------------------------------------------

    async def _open_tab(self) -> Page:
        page = await self.browser.context.new_page()
        self.opened.append(page)
        await self._prepare(page)
        return page

    async def _prepare(self, page: Page) -> None:
        if page in self._prepared:
            return
        self._prepared.add(page)
        # With vision on, images must load or screenshots show empty boxes.
        blocked = {"media"} if self.vision else BLOCKED_RESOURCES

        async def handler(route):
            if route.request.resource_type in blocked:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", handler)
        page.on("download", lambda d: self._downloads.append(d.url))

    async def _settle(self) -> None:
        busy = False
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            busy = True
        try:
            await self.page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            busy = True  # still loading, or a page that polls forever
        await self._wait_for_content(busy=busy)
        if await page_blocked(self.page):
            self._note_site(self.page.url, "captcha", "出现过人机验证，别连续快速打开很多页面")
            t0 = time.monotonic()
            await self.page.unroute("**/*")  # the CAPTCHA picture must load
            self._prepared.discard(self.page)
            await self.page.reload(wait_until="domcontentloaded")
            await wait_for_human(self.page, log=self.log)
            await self._prepare(self.page)
            self._paused_for += time.monotonic() - t0  # a person's time, not the research budget

    async def _wait_for_content(self, limit: float = 15.0, busy: bool = True) -> None:
        """Slow sites: the page is there but its results are still loading."""
        deadline = time.monotonic() + limit
        last, same, noted = -1, 0, False
        while True:
            try:
                length, loading = await asyncio.wait_for(self.page.evaluate(LOADING_JS), 5)
            except Exception:
                return
            if not loading and (length >= 200 or not busy):
                return
            same = same + 1 if length == last and not loading else 0
            if same >= 3 and length > 0:
                return  # a short page that has stopped changing
            if time.monotonic() >= deadline:
                return
            last = length
            if not noted:
                noted = True
                self.log("  (页面加载较慢，再等一会…等不及可以点「我来操作」)")
            await asyncio.sleep(0.8)

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

    async def _render(self, frame=None, prefix: str = "") -> dict:
        target = frame or self.page
        return await asyncio.wait_for(target.evaluate(RENDER_JS, {"prefix": prefix, "navCap": 15}), 20)

    async def _render_all(self) -> str:
        """Fresh text of the page and its frames, with element numbers (for find/read)."""
        snap = await self._render()
        self._rendered = snap["text"]
        parts = [snap["text"]]
        for i, frame in self._frames.items():
            try:
                fs = await self._render(frame, f"{i}-")
            except Exception:
                continue
            parts.append(f"[内嵌框架{i}]\n{fs['text']}")
        return "\n".join(parts)

    async def observe(self) -> str:
        page = self.page
        if self._is_pdf_url(page.url):
            try:
                summary = await asyncio.wait_for(self.read_pdf({"url": page.url}), 60)
            except Exception:
                summary = ""
            return f"当前页是PDF（浏览器里的PDF你翻不了页，用 pdf 动作按页读取）：{page.url}\n{summary}"
        try:
            snap = await self._render()
        except asyncio.TimeoutError:
            return f"页面还在加载，暂时读不到内容：{page.url}（可以换一个网站，或等一会再看）"
        except Exception as e:
            return f"读取页面失败：{e}"
        text = self._rendered = snap["text"]
        self._save(snap["url"], snap["title"], text)
        # Start where the page is scrolled to (after a scroll, show what's on screen).
        start = snap["vp"] if snap["vp"] > 300 else 0
        if start:
            cut = text.rfind("\n", max(0, start - 200), start)
            start = cut + 1 if cut >= 0 else start
        end = min(len(text), start + self.max_text)
        sc = snap["scroll"]
        screens = max(1, -(-sc["h"] // max(1, sc["v"])))
        at = min(screens, int(sc["y"] // max(1, sc["v"])) + 1)
        parts = [f"标题：{snap['title']}\n网址：{snap['url']}\n滚动位置：第{at}屏/共{screens}屏"]
        host = memory._host(snap["url"])
        if host and host not in self._noted_hosts:
            self._noted_hosts.add(host)
            notes = memory.site_notes(self.kb, host)
            if notes:
                parts.append("关于这个网站以前的经验：" + "；".join(notes[-4:]))
            if site_tips(snap["url"]):
                parts.append("这个网站的用法：" + site_tips(snap["url"]))
        if LOGIN_WALL.search(text[:6000]):
            parts.append("注意：这个页面提示要登录/订阅才能看全文。换一个能看到的来源（OA版本、其他数据库、文章摘要页的信息），"
                         "或者用户已登录的网站；实在需要就在 memory 里说明，用户可以点「我来操作」去登录")
        note = f"页面文字（共{len(text)}字"
        if start:
            note += f"；从当前屏幕位置开始显示，上面还有{start}字，需要时用 read（from=0）"
        note += f"；后面还有{len(text) - end}字，用 read（from={end}）继续读，或用 find 查关键词）：" if end < len(text) else "，已显示到末尾）："
        parts.append(f"{note}\n{text[start:end]}")
        self._window_end = end
        self._frames = {}
        for i, frame in enumerate(await self._content_frames(), start=1):
            try:
                fs = await self._render(frame, f"{i}-")
            except Exception:
                continue
            self._frames[i] = frame
            self._save(fs["url"], fs["title"], fs["text"])
            ftext = fs["text"]
            more = f"…（框架共{len(ftext)}字，用 find 查找）" if len(ftext) > 2000 else ""
            parts.append(f"\n内嵌框架{i}（{fs['url'][:80]}）：\n{ftext[:2000]}{more}")
        return "\n".join(parts)

    def _save(self, url: str, title: str, text: str) -> None:
        if self.kb is None or not text.strip() or url in self._saved or url.startswith(("about:", "chrome")):
            return
        self._saved.add(url)
        try:
            self.kb.add_document(url, [(None, _plain(text)[:60000])], title=title, kind="live", module="live")
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
        downloads = len(self._downloads)
        result = await self._act(a, kind, page, before)
        if len(self._downloads) > downloads and kind in ("goto", "click", "click_xy", "next_page"):
            # The site sent a file instead of a page (e.g. a PDF download button): read it.
            url = self._downloads[-1]
            text = await self.read_pdf({"url": url, **{k: a[k] for k in ("page", "find", "look") if k in a}})
            return f"下载了文件 {url}\n{text}"
        return result

    async def _act(self, a: dict, kind, page, before) -> str:
        if kind == "goto":
            url = str(a.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            try:
                resp = await goto(page, url, 40000)
            except PlaywrightTimeout:
                self._note_site(url, "slow", "打开很慢（40秒还没加载完），时间紧时优先用别的网站")
                await self._wait_for_content(5)
                return "页面加载很慢（40秒还没完全打开），先看已经显示出来的部分；是空白就换一个网站"
            ctype = (resp.headers.get("content-type", "") if resp else "").lower()
            if "pdf" in ctype or (resp is None and self._is_pdf_url(url)):
                return await self.read_pdf({"url": url})
            await self._settle()
            return "已打开"
        if kind == "search":
            q = quote_plus(str(a.get("query", "")))
            engine = str(a.get("engine", "bing")).lower()
            url = f"https://www.baidu.com/s?wd={q}" if engine == "baidu" else f"https://cn.bing.com/search?q={q}"
            await goto(page, url, 30000)
            await self._settle()
            return "已搜索"
        if kind == "click":
            el = self._element(a.get("ref")) if a.get("ref") not in (None, "") else await self._by_text(str(a.get("text", "")))
            href = await el.get_attribute("href", timeout=5000)
            sig = await self._signature()
            await self.pointer.to_element(page, el)
            try:
                await el.click(timeout=5000)
            except Exception:
                await el.evaluate("e => e.click()")  # covered by an overlay, or hidden styled input
            await asyncio.sleep(0.6)
            await self._follow_new_tab(before)
            if href and self._is_pdf_url(urljoin(page.url, href)):
                return await self.read_pdf({"url": urljoin(page.url, href)})
            await self._settle()
            return await self._changed(sig, "已点击")
        if kind == "hover":
            el = self._element(a.get("ref"))
            await self.pointer.to_element(page, el, click=False)
            await el.hover(timeout=5000)
            await asyncio.sleep(0.8)
            return "已悬停"
        if kind == "scroll":
            return await self.scroll(a)
        if kind == "next_page":
            found = await page.evaluate(NEXT_PAGE_JS)
            if found != "ok":
                await page.evaluate(SCROLL_JS, "bottom")  # pagers are often at the bottom, loaded late
                await asyncio.sleep(0.8)
                found = await page.evaluate(NEXT_PAGE_JS)
            if found == "last":
                return "已经是最后一页了（下一页按钮不可用）"
            if found != "ok":
                return "没找到下一页按钮；看看页面上有没有页码链接可以点，或者用 scroll 往下滑"
            sig = await self._signature()
            el = page.locator("[data-qp-next]").last
            await self.pointer.to_element(page, el)
            try:
                await el.click(timeout=5000)
            except Exception:
                await el.evaluate("e => e.click()")
            await asyncio.sleep(0.6)
            await self._follow_new_tab(before)
            await self._settle()
            await page.evaluate("() => window.scrollTo(0, 0)")
            return await self._changed(sig, "已翻到下一页")
        if kind == "type":
            el = self._element(a.get("ref"))
            await self.pointer.to_element(page, el)
            await el.fill(str(a.get("text", "")), timeout=5000)
            if a.get("enter"):
                sig = await self._signature()
                await el.press("Enter")
                await asyncio.sleep(0.6)
                await self._follow_new_tab(before)
                await self._settle()
                return await self._changed(sig, "已输入并回车")
            return "已输入"
        if kind == "select":
            el = self._element(a.get("ref"))
            option = str(a.get("option", ""))
            await self.pointer.to_element(page, el)
            try:
                await el.select_option(label=option, timeout=5000)
            except Exception:
                await el.select_option(value=option, timeout=5000)
            await self._settle()
            return "已选择"
        if kind == "flow":
            return await self.run_flow(a)
        if kind == "look":
            return await self.look(str(a.get("question", "描述这个页面")))
        if kind == "click_xy":
            sig = await self._signature()
            await self.pointer.to_xy(page, float(a.get("x", 0)), float(a.get("y", 0)))
            await page.mouse.click(float(a.get("x", 0)), float(a.get("y", 0)))
            await asyncio.sleep(0.6)
            await self._follow_new_tab(before)
            await self._settle()
            return await self._changed(sig, "已按坐标点击")
        if kind == "find":
            return await self.find_text(str(a.get("text", "")))
        if kind == "open_many":
            return await self.open_many(a)
        if kind == "count":
            return await self.count(a)
        if kind == "read":
            return await self.read_more(a.get("from"))
        if kind == "pdf":
            return await self.read_pdf(a)
        if kind == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
            await self._settle()
            return "已返回"
        return f"未知动作：{kind}"

    async def _by_text(self, text: str):
        """The smallest visible element showing exactly this text (else containing it)."""
        text = text.strip()
        if not text:
            raise ValueError("click 需要 ref 或 text")
        for exact in (True, False):
            for frame in [self.page.main_frame, *self._frames.values()]:
                loc = frame.get_by_text(text, exact=exact)
                try:
                    count = await loc.count()
                except Exception:
                    continue
                for i in range(count - 1, -1, -1):  # innermost/last match first
                    el = loc.nth(i)
                    if await el.is_visible():
                        return el
        raise ValueError(f"页面上没有文字为“{text}”的可见元素")

    async def _signature(self) -> str:
        """What the page looks like now, to tell whether an action did anything."""
        parts = [str(len(self.browser.context.pages)), self.page.url]
        for frame in [self.page.main_frame, *self._frames.values()]:
            try:
                parts.append(await asyncio.wait_for(frame.evaluate(SIGNATURE_JS), 5))
            except Exception:
                parts.append("?")
        return "#".join(parts)

    async def _changed(self, before: str, done: str) -> str:
        if await self._signature() != before:
            return done
        await asyncio.sleep(1.5)  # some sites update a moment later
        if await self._signature() != before:
            return done
        return (f"{done}，但页面没有任何变化：可能点错了元素（比如点了文字而不是按钮/链接）、"
                "要先填好或选中别的东西、或者内容在别的标签页；换一个元素或方法")

    async def scroll(self, a: dict) -> str:
        page = self.page
        if a.get("ref") not in (None, ""):
            el = self._element(a.get("ref"))
            await el.scroll_into_view_if_needed(timeout=5000)
            await page.evaluate("() => { window.__qpScrollBox = null; }")
            await asyncio.sleep(0.5)
            return "已滚动到该元素"
        direction = str(a.get("direction", "down")).lower()
        direction = direction if direction in ("up", "down", "top", "bottom") else "down"
        info = await page.evaluate(SCROLL_JS, direction)
        await asyncio.sleep(0.8)  # lazy-loaded lists fetch more when scrolled
        try:
            await page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass
        screens = max(1, -(-info["h"] // max(1, info["v"])))
        at = min(screens, int(info["y"] // max(1, info["v"])) + 1)
        if not info["moved"]:
            edge = "顶部" if direction in ("up", "top") else "底部"
            return f"已经在页面{edge}了，滚不动（第{at}屏/共{screens}屏）"
        return f"已{'向上' if direction in ('up', 'top') else '向下'}滚动，现在在第{at}屏/共{screens}屏"

    async def _ask_image(self, image: bytes, question: str, extra: str = "") -> str:
        """One vision call; turns vision off for the session if unsupported."""
        if not self.vision:
            return "视觉不可用（当前模型不支持看图），请用页面文字、find 或 pdf 的文字内容判断"
        system = "你在看一张网页或PDF页面的截图，回答用户的问题。只输出JSON：" \
                 '{"answer":"简洁的回答","x":0,"y":0}（x、y 只在问到某个元素的位置时填写，是该元素中心在截图上的像素坐标）'
        try:
            reply = await asyncio.to_thread(self.model.chat_json, system, f"{question}\n{extra}", 600, [image])
        except VisionUnsupported:
            self.vision = False
            try:
                self.model.vision_unsupported = True
            except Exception:
                pass
            self.log("  (this model can't take images - vision switched off)")
            return "视觉不可用（当前模型不支持看图），请用页面文字、find 或 pdf 的文字内容判断"
        out = f"看图结果：{reply.get('answer', '')}"
        if reply.get("x") and reply.get("y"):
            out += f"（位置 x={reply['x']}, y={reply['y']}，可用 click_xy 点击）"
        return out

    async def look(self, question: str) -> str:
        async with self.pointer.hidden(self.page):
            shot = await self.page.screenshot(type="jpeg", quality=60)
        size = self.page.viewport_size or {}
        return await self._ask_image(
            shot, question, f"截图尺寸 {size.get('width', '?')}x{size.get('height', '?')}，坐标原点在左上角。"
        )

    async def run_flow(self, a: dict) -> str:
        from .flows import replay

        name = str(a.get("name", ""))
        flow = self.kb.get_flow(name) if self.kb is not None else None
        if flow is None:
            return f"没有名为「{name}」的流程"
        params = a.get("params") if isinstance(a.get("params"), dict) else {}
        self.page = await replay(self.browser.context, self.page, flow, params, log=self.log, pointer=self.pointer)
        await self._prepare(self.page)
        return f"已运行流程「{name}」，参数 {json.dumps(params, ensure_ascii=False)}"

    async def _page_text(self) -> str:
        try:
            return await self._render_all()
        except Exception:
            return self._rendered

    async def find_text(self, needle: str) -> str:
        needles = [n.strip() for n in needle.split("|") if n.strip()]
        if not needles:
            return "find 需要 text"
        lines = [ln.strip() for ln in (await self._page_text()).splitlines() if ln.strip()]
        out = []
        for word in needles[:5]:
            hits = []
            for i, ln in enumerate(lines):
                if word.lower() in ln.lower():
                    hits.append(" / ".join(lines[max(0, i - 1) : i + 2])[:300])
                if len(hits) >= 15:
                    break
            out.append(f"找到{len(hits)}处“{word}”：\n" + "\n".join(hits) if hits else f"页面中没有“{word}”")
        return "\n".join(out)

    async def count(self, a: dict) -> str:
        """Count lines mentioning a text on the whole page (optionally between two headings)."""
        needle = str(a.get("text", "")).strip()
        start_at, end_at = str(a.get("from", "")).strip(), str(a.get("to", "")).strip()
        lines = [ln.strip() for ln in _plain(await self._page_text()).splitlines() if ln.strip()]
        lines = [re.sub(r"\s+", " ", ln) for ln in lines]
        region, note = lines, "整页"
        if start_at:
            i = next((j for j, ln in enumerate(lines) if start_at.lower() in ln.lower()), None)
            if i is None:
                return f"页面上没有“{start_at}”，没法确定从哪里开始数；换一个标题文字，或先 scroll/read 看看"
            region = lines[i + 1:]
            note = f"从“{start_at}”之后"
        if end_at:
            j = next((k for k, ln in enumerate(region) if end_at.lower() in ln.lower()), None)
            if j is not None:
                region = region[:j]
                note += f"到“{end_at}”之前"
        hits = [ln for ln in region if needle.lower() in ln.lower()] if needle else region
        total = sum(ln.lower().count(needle.lower()) for ln in hits) if needle else len(hits)
        listing = "\n".join(f"{n}. {ln[:120]}" for n, ln in enumerate(hits[:80], 1))
        more = f"\n…（只列出前80行）" if len(hits) > 80 else ""
        what = f"含“{needle}”的行" if needle else "非空行"
        return f"{note}：{what}共 {len(hits)} 行（“{needle}”一共出现 {total} 次）：\n{listing}{more}" if needle else \
            f"{note}：{what}共 {len(hits)} 行：\n{listing}{more}"

    async def read_more(self, start: object = None) -> str:
        try:
            await self._render_all()
        except Exception:
            pass
        text = self._rendered
        try:
            start = max(0, int(start)) if start is not None else self._window_end
        except (TypeError, ValueError):
            start = self._window_end
        if start >= len(text):
            return f"页面共{len(text)}字，已经读到末尾了"
        end = min(len(text), start + self.max_text)
        more = f"；后面还有，用 read（from={end}）继续" if end < len(text) else "，已读到末尾"
        return f"页面第{start}-{end}字（共{len(text)}字{more}）：\n{text[start:end]}"

    async def open_many(self, a: dict) -> str:
        """Load several pages at once in background tabs; report what each says about the keywords."""
        urls = [str(u).strip() for u in (a.get("urls") or []) if str(u).strip()][:5]
        urls = [u if u.startswith(("http://", "https://")) else "https://" + u for u in urls]
        if not urls:
            return "open_many 需要 urls（网址列表）"
        words = [w.strip() for w in str(a.get("find", "")).split("|") if w.strip()]
        self._kept = None
        results = await asyncio.gather(*(self._peek(i, url, words) for i, url in enumerate(urls, 1)))
        kept, self._kept = self._kept, None
        if kept is not None:
            # Stay on the first page read, so the next step can click and scroll there.
            old = self.page
            self.page = kept
            await kept.bring_to_front()
            if old is not None and old is not kept and (old.url in ("", "about:blank") or old.url.startswith("chrome")):
                try:
                    await old.close()
                except Exception:
                    pass
            results.append(f"（现在停在【1】{kept.url} 上，要细看、点击、翻页就直接在这一页操作；不要再用 open_many 反复打开同样的网址）")
        return "\n\n".join(results)

    async def _peek(self, i: int, url: str, words: list[str]) -> str:
        page = await self._open_tab()
        keep = False
        head = f"【{i}】{url}"
        try:
            try:
                resp = await goto(page, url, 40000)
            except PlaywrightTimeout:
                resp = None
            ctype = (resp.headers.get("content-type", "") if resp else "").lower()
            if "pdf" in ctype or self._is_pdf_url(page.url):
                return f"{head}\n" + await self.read_pdf({"url": page.url, "find": words[0] if words else ""})
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            for _ in range(12):  # results that arrive after the page itself
                try:
                    length, loading = await page.evaluate(LOADING_JS)
                except Exception:
                    break
                if length >= 200 and not loading:
                    break
                await asyncio.sleep(0.8)
            if await page_blocked(page):
                self._note_site(url, "captcha", "出现过人机验证，别连续快速打开很多页面")
                self.log(f"  (后台标签页要人机验证：{url[:70]}，已留在浏览器里)")
                return f"{head}\n这个页面要人机验证，没读到内容（标签页留着）；要看它就 goto 这个网址，程序会等用户完成验证"
            snap = await asyncio.wait_for(page.evaluate(RENDER_JS, {"prefix": "", "navCap": 15}), 20)
            text = _plain(snap["text"])
            self._save(snap["url"], snap["title"], snap["text"])
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            out = [f"{head}\n标题：{snap['title']}"]
            for word in words[:5]:
                hits = [" / ".join(lines[max(0, j - 1): j + 2])[:300]
                        for j, ln in enumerate(lines) if word.lower() in ln.lower()][:6]
                out.append(f"“{word}”：" + ("\n  ".join(hits) if hits else "页面中没有"))
            out.append("开头内容：" + " ".join(lines)[:900])
            if i == 1:
                keep = True
                self._kept = page
            return "\n".join(out)
        except Exception as e:
            return f"{head}\n打开失败：{type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}"
        finally:
            try:
                if not keep and not await page_blocked(page):
                    await page.close()
            except Exception:
                pass

    async def _fetch_in_page(self, url: str) -> bytes:
        """Download with the browser's own session (cookies, anti-bot checks passed)."""
        from base64 import b64decode

        origin = urlparse(url)
        pages = [self.page, *self.browser.context.pages]
        for page in pages:
            try:
                if page.is_closed() or urlparse(page.url).netloc != origin.netloc:
                    continue
                data = await asyncio.wait_for(page.evaluate(FETCH_JS, url), 60)
                return b64decode(data) if data else b""
            except Exception:
                continue
        return b""

    async def _pdf_bytes(self, url: str) -> bytes:
        if url in self._pdf_cache:
            return self._pdf_cache[url]
        data = b""
        try:
            resp = await self.browser.context.request.get(url, timeout=60000)
            data = await resp.body()
        except Exception:
            pass
        if not data.lstrip()[:5].startswith(b"%PDF"):
            # Blocked (Cloudflare etc.) or needs the login session: fetch from inside the site's own page.
            data = await self._fetch_in_page(url) or data
        if data.lstrip()[:5].startswith(b"%PDF"):
            self._pdf_cache[url] = data
        return data

    async def read_pdf(self, a: dict) -> str:
        url = str(a.get("url") or self.page.url)
        data = await self._pdf_bytes(url)
        try:
            doc = pdftools.open_pdf(data)
        except Exception:
            return (f"{url} 没能下载到PDF（可能被网站拦截或要登录）。可以打开论文页面点「PDF」按钮，"
                    "在PDF页面上再用 pdf 动作；或者看文章页面上写的页数等信息")
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
                if 1 <= n <= total and a.get("look"):
                    out.append(f"第{n}页 " + await self._ask_image(pdftools.page_png(doc, n), str(a["look"])))
                if 1 <= n <= total:
                    info = pdftools.page_info(doc[n - 1])
                    figs = "、".join(info.figures) or "无"
                    tabs = "、".join(info.tables) or "无"
                    out.append(
                        f"第{n}页（印刷页码 {info.label or '-'}）：图注 {len(info.figures)} 个（{figs}），表注 {len(info.tables)} 个（{tabs}），"
                        f"嵌入位图 {info.images} 张、矢量绘图 {info.drawings} 处（论文的图常是矢量图，数图以图注为准，"
                        f"没把握就加 look 看这一页），最后一个字“{info.last_char}”\n"
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
            for s in hints:
                parts.append(f"- 模块{s['module']} {s['title']}：{' '.join(s['urls'][:6])}")
                for url in s["urls"][:3]:
                    if site_tips(url):
                        parts.append(f"  （{memory._host(url)} 用法：{site_tips(url)}）")
                    notes = memory.site_notes(self.kb, url)
                    if notes:
                        parts.append(f"  （{memory._host(url)} 以前的经验：{'；'.join(notes[-3:])}）")
        if self._flows:
            parts.append("\n已录制的操作流程（一个动作就能自动完成多步操作，适合时优先用，比一步步点击快得多）：")
            for f in self._flows:
                example = {p["name"]: p["example"] for p in f.params}
                parts.append(f"- {f.summary()}\n  用法：{json.dumps({'action': 'flow', 'name': f.name, 'params': example}, ensure_ascii=False)}（把参数换成本题的值）")
        named = tips_for_question(q.stem)
        if named:
            parts.append("\n题目提到的数据库的用法（照着做最快）：")
            parts += [f"- {t}" for t in named]
        if self._known:
            parts.append(f"\n这道题以前做过并核实过：正确答案是 {self._known}。如果题目完全一样，直接 answer。")
        if self._lessons:
            parts.append("\n以前做错过的相似题的教训（别再犯同样的错）：")
            parts += [_clip(x, 500) for x in self._lessons]
        if self._recipes:
            parts.append("\n以前答对过的相似题的做法（可以照着做，网址和步骤可直接复用）：")
            parts += [_clip(r, 700) for r in self._recipes]
        if steps:
            parts.append("\n已做的操作：")
            for i, s in enumerate(steps, 1):
                parts.append(f"{i}. {json.dumps(s.action, ensure_ascii=False)} → {_clip(s.result, 300)}")
        if self._memory:
            parts.append(f"\n你上一步记下的要点：{self._memory}")
        if self._checks:
            parts.append("\n各选项目前的核实情况（你之前记下的）：")
            parts += [f"{k}{_mark(v)} {v}" for k, v in sorted(self._checks.items())]
            todo = self._unchecked(q)
            if todo and not force:
                parts.append(f"还没核实的：{'、'.join(todo)}——先把它们查清楚再 answer。")
        if self._previous:
            parts.append(f"\n上一轮时间到时给出的答案是 {self._previous}，用户觉得还不够确定，请继续核实（找到原文依据再 answer）。")
        parts.append(f"\n当前页面：\n{observation}")
        if force:
            parts.append("\n时间到了：现在必须输出 answer 动作，给出最可能的答案。")
        else:
            parts.append(f"\n剩余时间约{remaining:.0f}秒。输出下一步的JSON。")
        return "\n".join(parts)

    async def _decide(self, q: Question, steps: list[Step], observation: str, remaining: float, force: bool) -> list[dict]:
        """The model's next actions (1-3); a single invalid marker on failure."""
        prompt = self._prompt(q, steps, observation, remaining, force)
        t0 = time.monotonic()
        entry = {"t": round(time.time(), 1), "type": "model", "prompt": prompt}
        self.trace.append(entry)
        try:
            reply = await asyncio.to_thread(self.model.chat_json, self.system, prompt, 400)
            entry.update(reply=reply, seconds=round(time.monotonic() - t0, 1))
        except LLMError as e:
            entry.update(error=str(e), seconds=round(time.monotonic() - t0, 1))
            self.log(f"  (model: {str(e)[:80]})")
            if "JSON" in str(e):
                return [{"action": "_invalid", "error": "上一步输出不是JSON，请只输出一个JSON对象"}]
            # Timeouts and network hiccups: let the loop try again.
            return [{"action": "_invalid", "error": "模型请求失败，请直接给出下一步", "failed": True}]
        if not isinstance(reply, dict):
            return [{"action": "_invalid", "error": "输出必须是JSON对象"}]
        if reply.get("memory"):
            self._memory = str(reply["memory"])[:300]
        self._update_checks(q, reply.get("options"))
        acts = reply.get("actions")
        if isinstance(acts, list):
            acts = [a for a in acts if isinstance(a, dict) and a.get("action")][:3]
        elif reply.get("action"):
            acts = [reply]  # a bare single action is fine too
        else:
            acts = []
        return acts or [{"action": "_invalid", "error": "没有给出动作，请输出 actions"}]

    def _update_checks(self, q: Question, options: object) -> None:
        if not isinstance(options, dict):
            return
        before = dict(self._checks)
        for key, value in options.items():
            key = str(key).strip().upper()[:1] if str(key).strip()[:1].upper() in q.options else str(key).strip()
            if key in q.options or (q.kind == JUDGE and key):
                self._checks[key] = str(value).strip()[:160]
        if self._checks != before and self._checks:
            self.log("  核实：" + " ".join(f"{k}{_mark(v)}" for k, v in sorted(self._checks.items())))

    def _unchecked(self, q: Question) -> list[str]:
        if q.kind == JUDGE or not self._checks:
            return []
        return [k for k in q.options if not _mark(self._checks.get(k, "待查")) in ("✓", "✗")]

    def _load_recipes(self, q: Question) -> None:
        self._recipes = []
        self._flows = []
        self._lessons = []
        self._known = ""
        self._noted_hosts = set()
        if self.kb is None:
            return
        self._lessons = memory.lessons_for(self.kb, q.stem + " " + " ".join(q.options.values()))
        try:
            self._known = memory.known_answer(self.kb, q)
        except Exception:
            self._known = ""
        try:
            self._flows = self.kb.find_flows(q.stem + " " + " ".join(q.options.values()))
        except Exception:
            self._flows = []
        try:
            hits = self.kb.search(q.stem + " " + " ".join(q.options.values()), k=2, kind="recipe")
        except Exception:
            return
        self._recipes = [h.text for h in hits if h.score > 5]

    def _save_recipe(self, q: Question, steps: list[Step], final: dict, conf: float) -> None:
        """Remember how a confidently answered question was solved (verified later by grading)."""
        if self.kb is None or conf < 0.7:
            return
        try:
            memory.save_recipe(self.kb, q, steps, final)
        except Exception:
            pass

    def _note_site(self, url: str, key: str, note: str) -> None:
        try:
            memory.add_site_note(self.kb, url, key, note)
        except Exception:
            pass

    def _remember(self, text: str) -> None:
        """Everything read during research, for checking quoted evidence."""
        if text:
            self._seen.append(_plain(text))
            while sum(len(t) for t in self._seen) > 800_000 and len(self._seen) > 1:
                self._seen.pop(0)

    def _evidence_ok(self, action: dict, steps: list[Step]) -> bool:
        evidence = str(action.get("evidence") or "")
        if not evidence:
            return True
        if any(s.action.get("action") == "look" or s.action.get("look") for s in steps):
            return True  # read from a screenshot: can't be checked against text
        return evidence_found(evidence, "\n".join(self._seen + [_plain(self._rendered)]))

    # --- buttons on the answering page ----------------------------------------------

    async def _interruptible(self, coro):
        """Await coro, unless 我来操作 / 停止 is pressed first (then cancel it)."""
        task = asyncio.ensure_future(coro)
        if self.control is None:
            return await task
        waiter = asyncio.ensure_future(self.control.changed.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return _INTERRUPTED

    async def _user_page(self, known: set) -> Page:
        """The tab the person left in front: a tab they opened, else ours."""
        pages = [p for p in self.browser.context.pages if not p.is_closed()]
        visible = []
        for p in pages:
            try:  # a tab still waiting for its server can't answer; skip it
                if await asyncio.wait_for(p.evaluate("() => document.visibilityState === 'visible'"), 1.5):
                    visible.append(p)
            except Exception:
                continue
        new = [p for p in pages if p not in known]
        ours = self.page if self.page is not None and not self.page.is_closed() else None
        for group in ([p for p in new if p in visible], [ours] if ours in visible else [], visible, new, [ours] if ours else [], pages):
            if group:
                return group[-1]
        return await self._open_tab()

    async def _wait_for_user(self, steps: list[Step]) -> str:
        self.log("  ⏸ 已暂停：请在浏览器里自己操作（等页面加载完、翻到有答案的地方、登录等），好了点「继续作答」")
        known = set(self.browser.context.pages)
        t0 = time.monotonic()
        await self.control.wait_resumed()
        self._paused_for += time.monotonic() - t0
        return await self._take_over_from_user(steps, known)

    async def _take_over_from_user(self, steps: list[Step], known: set) -> str:
        self.page = await self._user_page(known)
        await self._prepare(self.page)
        url = self.page.url
        self.log(f"  ▶ 继续：从你当前的页面接着找 {url[:90]}")
        steps.append(Step({"action": "用户手动操作"},
                          f"用户自己操作了浏览器，现在停在 {url}。这一页很可能就有答案，先仔细读（需要时用 find / read）"))
        return await self.observe()

    # --- the loop ------------------------------------------------------------------

    async def run(self, q: Question, budget: float = 75, max_steps: int | None = None, close: bool = False,
                  resume: bool = False) -> LiveResult:
        """Research q in the browser; resume=True keeps going from where the last run (or the person) left off."""
        start = time.monotonic()
        self._paused_for = 0.0
        max_steps = max_steps or max(15, int(budget / 5))
        if resume and self.page is not None:
            steps = self._steps
            self.log(f"  继续查找（再查 {budget:.0f} 秒）")
            observation = await self._take_over_from_user(steps, set(self.browser.context.pages))
        else:
            self._memory = ""
            self._previous = ""
            self._seen = []
            self._checks = {}
            self._load_recipes(q)
            if self._recipes:
                self.log(f"  (found {len(self._recipes)} saved approach(es) for similar questions)")
            self.page = await self._open_tab()
            await self.page.bring_to_front()
            steps = self._steps = []
            observation = "（空白页，还没有打开任何网站）"
        final: dict | None = None
        failures = 0
        n = 0
        self._checked_evidence = False
        try:
            while True:
                if self.control is not None:
                    if self.control.paused:
                        observation = await self._wait_for_user(steps)
                    self.control.changed.clear()
                    if self.control.stop:
                        self.log("  ■ 停止查找，用已经看到的内容作答")
                n += 1
                self._remember(observation)
                elapsed = time.monotonic() - start - self._paused_for
                stopped = self.control is not None and self.control.stop
                force = stopped or elapsed > budget - 6 or n >= max_steps
                if force:
                    acts = await self._decide(q, steps, observation, 0, True)
                    if not any(a.get("action") == "answer" for a in acts):
                        acts = await self._decide(q, steps, observation, 0, True)
                    final = next((a for a in acts if a.get("action") == "answer"), acts[0])
                    break
                acts = await self._interruptible(self._decide(q, steps, observation, budget - elapsed, False))
                if acts is _INTERRUPTED:
                    n -= 1
                    continue
                if acts[0].get("action") == "_invalid":
                    failures = failures + 1 if acts[0].get("failed") else 0
                    if failures >= 3:
                        self.log("  (model unavailable - giving up on live research)")
                        break
                    steps.append(Step({"action": "（无效输出）"}, acts[0].get("error", "")))
                    continue
                failures = 0
                last_kind = None
                interrupted = False
                for j, action in enumerate(acts):
                    kind = action.get("action")
                    if kind == "answer":
                        if not self._checked_evidence and budget - elapsed > 15 and not self._evidence_ok(action, steps):
                            # Once per question: a quote that isn't on any page read is checked again.
                            self._checked_evidence = True
                            self.log(f"  [{n}] (答案 {action.get('answer')} 的证据在看过的页面里找不到原文，再核实一下)")
                            steps.append(Step({"action": "answer待核实", "answer": action.get("answer")},
                                              "证据核对没通过：evidence 要逐字抄页面上的原文，而你给的在看过的页面里找不到。"
                                              "用 find 在页面上找到原文（或 read 继续读、打开详情页）后再 answer；确实找不到就降低 confidence"))
                            break
                        final = action
                        break
                    label = f"[{n}{'abc'[j] if len(acts) > 1 else ''}]"
                    repeat = _repeats(action, steps)
                    if repeat:
                        self.log(f"  {label} (skipped repeat) {_describe(action)}")
                        steps.append(Step(action, f"重复操作，已跳过：{repeat}。换一个方法，比如打开目录中的官方网站、换关键词，或根据已有信息直接 answer。"))
                        break
                    self.log(f"  {label} {_describe(action)}")
                    t_act = time.monotonic()
                    try:
                        result = await self._interruptible(
                            asyncio.wait_for(self.act(action), timeout=120 if kind in ("flow", "open_many") else 60))
                    except Exception as e:
                        result = f"失败：{type(e).__name__}: {str(e).splitlines()[0][:150] if str(e) else ''}"
                    self.trace.append({"t": round(time.time(), 1), "type": "action", "action": action,
                                       "result": result if isinstance(result, str) else "被用户打断",
                                       "seconds": round(time.monotonic() - t_act, 1),
                                       "url": self.page.url if self.page is not None and not self.page.is_closed() else ""})
                    if result is _INTERRUPTED:
                        steps.append(Step(action, "被用户打断"))
                        interrupted = True
                        break
                    steps.append(Step(action, result))
                    self._remember(result)
                    if kind == "goto" and result.startswith("失败") and "ERR_" in result:
                        err = re.search(r"ERR_[A-Z_]+", result).group(0)
                        self._note_site(str(action.get("url", "")), "fail", f"打开失败（{err}），可能要校园网/登录，或网站不稳定")
                    last_kind = kind
                    if result.startswith("失败") or kind in PAGE_CHANGING or (kind == "type" and action.get("enter")):
                        break  # element numbers are stale now; look at the page again
                if final is not None:
                    break
                if interrupted:
                    continue
                if last_kind in ("find", "pdf", "read", "open_many", "count"):
                    observation = f"（仍在 {self.page.url}）\n{steps[-1].result}"
                else:
                    observation = await self.observe()
        finally:
            urls = [s.action.get("url") for s in steps if s.action.get("url")]
            try:
                last_url = self.page.url if self.page is not None else ""
            except Exception:
                last_url = ""
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
        final_url = self.page.url if self.page is not None and not self.page.is_closed() else ""
        todo = self._unchecked(q) if answer and q.kind == MULTI else []
        if todo:
            conf = min(conf, 0.7)
            reason = f"（选项 {'、'.join(todo)} 还没核实）" + reason
        if answer and final.get("evidence") and not self._evidence_ok(final, steps):
            conf = min(conf, 0.6)
            reason = "（给出的证据没在看过的页面原文中找到，可能不准）" + reason
        if answer and unreliable(final_url or last_url):
            conf = min(conf, 0.5)
            reason = "（依据来自题库/答案分享网站，可能不准，建议到官方网站核实）" + reason
        if answer:
            self._save_recipe(q, steps, final, conf)
            self._previous = f"{answer}（把握 {conf:.2f}）"
        ans = Answer(answer, conf, reason=reason, seconds=time.monotonic() - start)
        return LiveResult(ans, steps, urls)


PAGE_CHANGING = {"goto", "search", "click", "back", "flow", "scroll", "next_page", "hover", "click_xy"}

# Question banks and answer-sharing sites: often wrong, never the only proof.
UNRELIABLE_HOSTS = (
    "itihey.com", "zhidao.baidu.com", "wenku.baidu.com", "wen.baidu.com", "zuoyebang.com",
    "docin.com", "doc88.com", "shangxueba.com", "asklib.com", "examcoo.com", "ppkao.com",
    "mayiwenku.com", "renrendoc.com", "book118.com", "jingyan.baidu.com", "tiku", "daan",
)


def unreliable(url: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(url or "").netloc.lower()
    return any(h in host for h in UNRELIABLE_HOSTS)


def _repeats(action: dict, steps: list[Step]) -> str:
    """A goto/search identical to an earlier one only loops; say which."""
    kind = action.get("action")
    if kind == "open_many":
        mine = sorted(str(u).rstrip("/") for u in action.get("urls") or [])
        for s in steps:
            if s.action.get("action") == "open_many" and sorted(str(u).rstrip("/") for u in s.action.get("urls") or []) == mine:
                return "之前已经同时打开过这些网址"
        if sum(s.action.get("action") == "open_many" for s in steps[-3:]) >= 2:
            return "已经连续用了两次 open_many；接下来 goto 到最相关的那个页面，在页面上点击、检索、翻页细看"
        return ""
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
