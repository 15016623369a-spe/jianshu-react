"""Chromium smoke test for policy, chat, durable approvals, and file analysis."""
import os
from uuid import uuid4

from playwright.sync_api import Page, expect, sync_playwright


def check_policy_exploration(page: Page, origin: str) -> None:
    page.goto(f"{origin}/?session={uuid4()}&workspace=policy-exploration", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="政策探索实验")).to_be_visible(timeout=20000)

    def select(field: str, label: str) -> None:
        control = page.get_by_role("combobox", name=field)
        selectbox = page.get_by_test_id("stSelectbox").filter(has=control)
        selectbox.scroll_into_view_if_needed()
        selectbox.get_by_role("button", name="Open").click()
        page.get_by_role("option", name=label, exact=True).click()
        expect(control).to_have_value(label)

    run = page.get_by_role("button", name="运行政策探索")
    amount = page.get_by_test_id("stMetricValue")
    expect(page.get_by_role("combobox", name="执行方式")).to_have_value("自动 · 固定快路径 + 缓存计划 + 重规划")
    run.click()
    expect(amount).to_have_text("¥292.80", timeout=20000)
    page.get_by_text("查看计划与执行", exact=True).click()
    expect(page.get_by_text("实际路径 · 固定快路径", exact=True)).to_be_visible()
    expect(page.get_by_text("0 次模型调用", exact=False)).to_be_visible()

    select("选择示例", "先找指定标题，再找当期公告")
    expect(page.get_by_role("spinbutton", name="year, 起始日期")).to_have_text("2027")
    select("执行方式", "原固定流程 · 最多查一份公告")
    run.click()
    expect(page.get_by_text("可切换到自动模式，或用确定性遍历继续核查多层引用。", exact=True)).to_be_visible()
    expect(amount).to_have_count(0)

    select("执行方式", "确定性遍历 · 支持多层公告")
    run.click()
    expect(amount).to_have_text("¥211.00", timeout=20000)
    page.get_by_text("查看政策、公告和 SQL 证据", exact=True).click()
    expect(page.get_by_text("第 2 层引用", exact=True)).to_be_visible()
    expect(page.get_by_text("华南家电一月结算指引", exact=True)).to_be_visible()
    expect(page.get_by_text("家电一月争议清算执行公告", exact=True)).to_be_visible()
    expect(page.get_by_text("数据表 · 支付冲正（chargebacks）", exact=True)).to_be_visible()
    expect(page.get_by_text("出处 app/hard_policy_corpus/bulletins.md", exact=False)).to_have_count(2)

    select("选择示例", "公告循环引用 · 检查拒答")
    expect(page.get_by_role("textbox", name="产品")).to_have_value("蓝牙耳机")
    run.click()
    expect(page.get_by_text("公告引用形成循环，无法确认最终口径。", exact=True)).to_be_visible()
    expect(amount).to_have_count(0)
    print("PASS cited multi-hop policy retrieval, original fixed-flow boundary, and cyclic-referral refusal", flush=True)


def main():
    session = uuid4()
    origin = os.getenv("RAG_TEST_UI_URL", "http://127.0.0.1:8501")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
        try:
            exploration = browser.new_page(viewport={"width": 1280, "height": 900})
            check_policy_exploration(exploration, origin)
            exploration.close()

            policy = browser.new_page(viewport={"width": 1280, "height": 900})
            policy.goto(f"{origin}/?session={uuid4()}&workspace=policy", wait_until="domcontentloaded")
            expect(policy.get_by_role("heading", name="政策问数")).to_be_visible(timeout=20000)
            policy.get_by_role("button", name="按政策计算净营收").click()
            expect(policy.get_by_text("¥477.00", exact=True)).to_be_visible(timeout=20000)
            first = policy.get_by_text("2026-10-01 至 2026-10-15 · ¥350.00", exact=True)
            second = policy.get_by_text("2026-10-15 至 2026-11-01 · ¥127.00", exact=True)
            first.click()
            expect(policy.get_by_text("退款政策 · 2026 年 10 月上半月版", exact=False)).to_be_visible()
            second.click()
            expect(policy.get_by_text("退款政策 · 2026 年 10 月修订版", exact=False)).to_be_visible()
            expect(policy.get_by_text("绑定参数 [300,", exact=False)).to_be_visible()
            policy.close()
            print("PASS cross-version policy revenue and cited SQL", flush=True)

            starter = browser.new_page(viewport={"width": 1280, "height": 900})
            starter.goto(f"{origin}/?session={uuid4()}", wait_until="domcontentloaded")
            expect(starter.get_by_role("heading", name="和数据对话")).to_be_visible(timeout=20000)
            starter_messages = starter.get_by_test_id("stChatMessage")
            expect(starter_messages).to_have_count(0)
            example = starter.get_by_role("button", name="上月净营收", exact=True)
            example.click(timeout=20000)
            expect(starter_messages.last.get_by_text("¥475.00", exact=False)).to_be_visible(timeout=20000)
            starter.reload(wait_until="domcontentloaded")
            expect(starter_messages.last.get_by_text("¥475.00", exact=False)).to_be_visible(timeout=20000)
            starter.close()
            print("PASS suggested question and durable answer", flush=True)

            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"{origin}/?session={session}", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="和数据对话")).to_be_visible(timeout=20000)
            expect(page.get_by_test_id("stAppDeployButton")).to_have_count(0)
            composer = page.get_by_placeholder("输入你的问题")
            messages = page.get_by_test_id("stChatMessage")

            def ask(question, expected):
                composer.fill(question)
                composer.press("Enter")
                expect(messages.last.get_by_text(expected, exact=False)).to_be_visible(timeout=20000)

            ask("9 月营收是多少？", "还需确认 · 是否扣除退款")
            page.reload(wait_until="domcontentloaded")
            expect(messages.first.get_by_text("9 月营收是多少？", exact=True)).to_be_visible(timeout=20000)
            ask("十月", "时间 · 2026-10")
            ask("净营收", "¥475.00")
            ask("只看华南", "¥175.00")
            expect(messages.last.get_by_text("基于上一轮 · 2026-10 净营收", exact=True)).to_be_visible()
            expect(messages).to_have_count(8)
            print("PASS dialogue clarification and history", flush=True)

            ask("将订单 1001 的备注改为浏览器验收", "新备注「浏览器验收」")
            page.get_by_role("button", name="批准更新", exact=True).click()
            expect(messages.last.get_by_text("已更新备注", exact=False)).to_be_visible(timeout=20000)
            page.reload(wait_until="domcontentloaded")
            expect(messages.last.get_by_text("已更新备注", exact=False)).to_be_visible(timeout=20000)
            print("PASS approval and durable result", flush=True)

            ask("列出数据库的所有表", "演示库只暴露 orders 表")
            expect(messages.last.get_by_text("BIRD 数据源", exact=False)).to_be_visible()

            page.get_by_text("文件分析", exact=True).click()
            live = page.get_by_role("checkbox", name="用已配置的模型理解分析任务")
            expect(live).to_be_visible(timeout=20000)
            if live.is_checked():
                live.press("Space")
                expect(live).not_to_be_checked()
            uploader = page.locator('input[type="file"]')
            expect(uploader).to_be_attached(timeout=20000)
            uploader.set_input_files({"name": "smoke.csv", "mimeType": "text/csv",
                                      "buffer": "城市,收入\n甲,8\n乙,9\n丙,10\n丁,11\n戊,12\n己,110\n".encode()})
            expect(page.get_by_text("解析出的表头", exact=False)).to_be_visible(timeout=20000)
            page.get_by_role("textbox", name="你想分析什么？").fill("有没有异常值并画图")
            page.get_by_role("button", name="开始分析", exact=True).click()
            expect(page.get_by_text("疑似异常值", exact=False).first).to_be_visible(timeout=30000)
            expect(page.get_by_test_id("stPlotlyChart")).to_be_visible(timeout=20000)
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_text("已保存文件 · smoke.csv", exact=True)).to_be_visible(timeout=20000)
            expect(page.get_by_role("textbox", name="你想分析什么？")).to_have_value("有没有异常值并画图")
            expect(page.get_by_test_id("stPlotlyChart")).to_be_visible(timeout=20000)

            reopened = browser.new_page(viewport={"width": 1280, "height": 900})
            reopened.goto(page.url, wait_until="domcontentloaded")
            expect(reopened.get_by_test_id("stPlotlyChart")).to_be_visible(timeout=20000)
            with reopened.expect_download(timeout=15000) as downloaded:
                reopened.get_by_role("button", name="下载 CSV", exact=True).click()
            assert "110" in downloaded.value.path().read_bytes().decode("utf-8-sig")
            question = reopened.get_by_role("textbox", name="你想分析什么？")
            question.fill("帮我统计一下")
            question.press("Tab")
            reopened.get_by_role("button", name="开始分析", exact=True).click()
            expect(reopened.get_by_text("帮我统计一下", exact=True)).to_be_visible(timeout=20000)
            expect(reopened.get_by_test_id("stPlotlyChart")).to_have_count(0)
            reopened.get_by_role("button", name="移除已保存文件和结果").click()
            expect(reopened.get_by_role("button", name="开始分析", exact=True)).to_be_disabled(timeout=20000)
            reopened.reload(wait_until="domcontentloaded")
            expect(reopened.get_by_text("已保存文件 · smoke.csv", exact=True)).to_have_count(0)
            print("PASS file result, chart, download, re-analysis and deletion after reopening", flush=True)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
