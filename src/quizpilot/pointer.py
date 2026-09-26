"""A visible mouse pointer drawn in the page while the AI works.

Playwright clicks without moving the real mouse, so you can't see where
the agent is about to click. This draws an arrow that glides to each
target, outlines the element and ripples on click. It ignores the mouse
(pointer-events: none), has no text, and is hidden for screenshots, so it
never changes what the agent clicks or reads.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

POINTER_JS = r"""
({x, y, fromX, fromY, click, box, ms}) => {
  let root = document.getElementById('__qp_pointer');
  if (!root) {
    root = document.createElement('div');
    root.id = '__qp_pointer';
    root.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    root.innerHTML =
      '<div style="position:fixed;left:0;top:0;width:26px;height:26px;pointer-events:none;will-change:transform;' +
      'filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))">' +
      '<svg width="26" height="26" viewBox="0 0 26 26"><path d="M3 2 L3 21 L8.5 16 L12.5 24.5 L16 23 L12 14.8 L19.5 14.5 Z" ' +
      'fill="#ff4d1a" stroke="#ffffff" stroke-width="1.6" stroke-linejoin="round"/></svg></div>';
    (document.body || document.documentElement).appendChild(root);
    root.firstChild.style.transform = `translate(${fromX - 3}px, ${fromY - 2}px)`;
    root.firstChild.getBoundingClientRect();  // start from the last position, then glide
  }
  const arrow = root.firstChild;
  arrow.style.display = '';
  arrow.style.transition = `transform ${ms}ms cubic-bezier(.3,.7,.4,1)`;
  arrow.style.transform = `translate(${x - 3}px, ${y - 2}px)`;
  const flash = (css, life) => {
    const d = document.createElement('div');
    d.style.cssText = 'position:fixed;pointer-events:none;box-sizing:border-box;' + css;
    root.appendChild(d);
    setTimeout(() => d.remove(), life);
    return d;
  };
  if (box) {
    const d = flash(`left:${box.x - 3}px;top:${box.y - 3}px;width:${box.width + 6}px;height:${box.height + 6}px;` +
      'border:2px solid #ff4d1a;border-radius:5px;background:rgba(255,77,26,.08);transition:opacity .5s;', 1600);
    setTimeout(() => { d.style.opacity = '0'; }, 1000);
  }
  if (click) {
    setTimeout(() => {
      const r = flash(`left:${x - 16}px;top:${y - 16}px;width:32px;height:32px;border-radius:50%;` +
        'border:3px solid #ff4d1a;transform:scale(.3);opacity:1;transition:transform .45s ease-out,opacity .45s;', 600);
      r.getBoundingClientRect();
      r.style.transform = 'scale(1.3)';
      r.style.opacity = '0';
    }, ms);
  }
}
"""

HIDE_JS = "(show) => { const p = document.getElementById('__qp_pointer'); if (p) p.style.display = show ? '' : 'none'; }"


class Pointer:
    """Remembers where the pointer is so it glides on from there, even across pages."""

    def __init__(self, enabled: bool = True, glide_ms: int = 350):
        self.enabled = enabled
        self.glide_ms = glide_ms
        self.x, self.y = 160.0, 120.0

    async def to_xy(self, page, x: float, y: float, *, click: bool = True, box: dict | None = None) -> None:
        if not self.enabled:
            return
        try:
            await asyncio.wait_for(page.evaluate(POINTER_JS, {
                "x": x, "y": y, "fromX": self.x, "fromY": self.y, "click": click, "box": box, "ms": self.glide_ms,
            }), 3)
        except Exception:
            return  # only a visual aid: never let it break an action
        self.x, self.y = x, y
        await asyncio.sleep(self.glide_ms / 1000 + 0.05)

    async def to_element(self, page, locator, *, click: bool = True) -> None:
        """Glide to the middle of an element (in any frame) and outline it."""
        if not self.enabled:
            return
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
            box = await locator.bounding_box(timeout=3000)
        except Exception:
            return
        if not box:
            return
        await self.to_xy(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, click=click, box=box)

    @asynccontextmanager
    async def hidden(self, page):
        """Keep the pointer out of screenshots the model looks at."""
        try:
            await page.evaluate(HIDE_JS, False)
        except Exception:
            pass
        try:
            yield
        finally:
            try:
                await page.evaluate(HIDE_JS, True)
            except Exception:
                pass
