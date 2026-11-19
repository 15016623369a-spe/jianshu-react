"""Record a short silent portfolio screencast of the V3 investigation workspace."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

A1 = "2027 年 1 月华南电水壶净营收是多少？如果都按 2026 年 10 月口径重算呢？"
INTRO = """
<html><body style="margin:0;background:#0d2b21;color:#f4f1e8;font-family:system-ui,-apple-system,sans-serif;
display:flex;flex-direction:column;justify-content:center;padding:8vw;height:100vh;box-sizing:border-box">
<div style="font-size:14px;letter-spacing:.28em;opacity:.65">见数 · PORTFOLIO DEMO</div>
<div style="font-size:58px;font-weight:750;line-height:1.15;margin-top:28px">从问题到可审计答案</div>
<div style="font-size:24px;line-height:1.6;opacity:.85;margin-top:26px;max-width:900px">
Evidence-gated Agentic RAG<br>
政策 · 手册 · 零售数据 · 版本 · 引用 · 拒答
</div>
<div style="font-size:16px;opacity:.6;margin-top:60px">150 题模型兜底 150/150 · 错误出数 0 · 301 tests</div>
</body></html>
"""


def _caption(page, text: str) -> None:
    page.evaluate(
        """(text) => {
            let node = document.getElementById('demo-caption');
            if (!node) {
                node = document.createElement('div');
                node.id = 'demo-caption';
                node.style.cssText = 'position:fixed;left:24px;bottom:24px;z-index:999999;background:#0d2b21;' +
                    'color:#f4f1e8;padding:12px 18px;border-radius:10px;font:600 15px system-ui;' +
                    'box-shadow:0 8px 30px rgba(0,0,0,.25)';
                document.body.appendChild(node);
            }
            node.textContent = text;
        }""",
        text,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:28502")
    parser.add_argument("--output", type=Path, default=Path("examples/demo/v3-portfolio-demo.mp4"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=directory,
                                      record_video_size={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_content(INTRO)
        page.wait_for_timeout(2600)
        page.goto(f"{args.url}/?workspace=policy-exploration&session={uuid.uuid4()}", wait_until="domcontentloaded")
        page.wait_for_timeout(2400)
        page.locator("textarea").first.fill(A1)
        page.get_by_role("button", name="开始调查").click()
        page.get_by_text("实际减统一", exact=True).first.wait_for(timeout=90000)
        _caption(page, "A1 · 单月跨口径：211.00 / 220.00 / −9.00")
        page.wait_for_timeout(3200)
        page.get_by_text("核对实际口径的条款和 SQL", exact=True).click()
        page.wait_for_timeout(2600)
        page.get_by_text("核对统一口径的条款和 SQL", exact=True).click()
        page.wait_for_timeout(2200)
        page.get_by_role("tab", name="调查历史").click()
        page.wait_for_timeout(2400)
        _caption(page, "历史 · 保存当时金额、证据和执行摘要")
        page.wait_for_timeout(2200)
        page.get_by_role("tab", name="系统状态").click()
        page.wait_for_timeout(1800)
        _caption(page, "系统状态 · 模型 / 数据源 / rerank")
        page.wait_for_timeout(1800)
        page.close()
        context.close()
        browser.close()
        videos = list(Path(directory).glob("*.webm"))
        if not videos:
            raise RuntimeError("Playwright did not produce a video")
        subprocess.run([
            "ffmpeg", "-y", "-i", str(videos[0]), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(args.output),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
