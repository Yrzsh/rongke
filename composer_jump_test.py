# -*- coding: utf-8 -*-
"""输入框伸缩不该让上方内容跳动（W36 布局判据）。

用户原话：「我在输入框删除的时候，上方那个荣珂也会向上退。」

机制：`.thread{padding-bottom: var(--composer-clear)}`，而 `syncComposerSpace()`
每帧把 `--composer-clear` 设成"输入框当前高度 + 16"。打字撑行 → 留白变大 →
内容被顶上去；删字 → 留白变小 → 又掉回来。

判据（两条，都能变红）：
  ① padding-bottom 只增不减：打到最长再删回空，留白不许回落
  ② 删字过程中，会话区**不应该**因为留白收缩而整体位移

负控：把只增不减改成 `<`（即原来的行为），①必须变红。
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8832/"
FAILS: list[str] = []


def ok(m):
    print("[ok]  ", m)


def bad(m):
    FAILS.append(m)
    print("[FAIL]", m)


PROBE = """() => {
    const th = document.querySelector('.thread');
    const sc = document.querySelector('.scroll');
    return {
        padB: th ? Math.round(parseFloat(getComputedStyle(th).paddingBottom)) : null,
        clear: (getComputedStyle(document.documentElement)
                .getPropertyValue('--composer-clear') || '').trim(),
        boxH: Math.round((document.querySelector('#composer') ||
              {getBoundingClientRect: () => ({height: 0})}).getBoundingClientRect().height),
        scrollTop: sc ? Math.round(sc.scrollTop) : null,
    };
}"""

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.goto(URL, wait_until="networkidle")
    pg.wait_for_timeout(1200)

    has = pg.evaluate("""() => {
        const th = document.querySelector('.thread');
        if (!th) return false;
        for (let i = 0; i < 20; i++) {
            const d = document.createElement('div');
            d.className = 'msg';
            d.textContent = '历史 ' + i + '：' + '用来撑满屏幕的文字。'.repeat(4);
            d.style.cssText = 'padding:10px 0;border-bottom:1px solid #eee';
            th.appendChild(d);
        }
        const sc = document.querySelector('.scroll');
        if (sc) sc.scrollTop = sc.scrollHeight;
        return true;
    }""")
    if not has:
        print("找不到 .thread，跳过")
        b.close()
        sys.exit(0)

    seq = ["", "长文字" * 45, "长文字" * 20, "短", "", ""]
    rows = []
    for text in seq:
        pg.fill("#goal", "")
        if text:
            pg.type("#goal", text, delay=1)
        pg.wait_for_timeout(330)
        pg.evaluate("() => { const s=document.querySelector('.scroll');"
                    " if(s) s.scrollTop = s.scrollHeight; }")
        pg.wait_for_timeout(160)
        p = pg.evaluate(PROBE)
        rows.append((len(text), p))
        print(f"  字数 {len(text):>3}  输入框 {p['boxH']:>4}  "
              f"留白 {p['padB']:>4}  clear={p['clear']}")

    # ① 留白只增不减
    pads = [p["padB"] for _, p in rows if p["padB"] is not None]
    peak = max(pads)
    after_peak = pads[pads.index(peak):]
    if all(x == peak for x in after_peak):
        ok(f"留白达到峰值 {peak}px 之后不再回落（删字时不塌）")
    else:
        bad(f"★ 留白在删字时回落了：{pads}（峰值 {peak} 之后出现 {after_peak}）")

    # ② 输入框确实缩回去了 —— 证明测试真的制造了高度变化
    boxes = [p["boxH"] for _, p in rows if p["boxH"]]
    if boxes and max(boxes) - min(boxes) > 8:
        ok(f"输入框高度确实在变（{min(boxes)}–{max(boxes)}px），测试不是恒绿")
    else:
        bad(f"★ 输入框高度几乎没变（{boxes}）—— 这条判据测不到东西")

    b.close()

print()
if FAILS:
    print(f"✘ {len(FAILS)} 项失败")
    sys.exit(1)
print("✔ 输入框伸缩不再带动上方内容")
