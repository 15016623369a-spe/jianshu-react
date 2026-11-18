"""Chromium smoke test for policy, chat, durable approvals, and file analysis."""
import os
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Page, expect, sync_playwright


def check_policy_investigation(page: Page, origin: str) -> None:
    page.goto(f"{origin}/?session={uuid4()}", wait_until="domcontentloaded")
    panel = page.get_by_role("tabpanel", name="自然语言调查")
    submit = panel.get_by_role("button", name="开始调查")
    expect(submit).to_be_visible(timeout=20000)
    submit.click()

    metrics = panel.get_by_test_id("stMetricValue")
    expect(metrics).to_have_text(["−¥46.40", "−¥40.80", "−¥5.60"], timeout=20000)
    expect(panel.get_by_text("¥246.40", exact=True)).to_be_visible()
    expect(panel.get_by_text("¥252.00", exact=True)).to_be_visible()
    expect(panel.get_by_text("统一口径沿用 2026 年 10 月的退款规则重算同一批订单。", exact=False)).to_be_visible()
    panel.get_by_text("核对实际口径的条款和 SQL", exact=True).click()
    expect(panel.get_by_text("出处 app/hard_policy_corpus/bulletins.md", exact=False).first).to_be_visible()
    expect(panel.get_by_text("绑定参数", exact=False).first).to_be_visible()
    panel.get_by_text("核对统一口径的条款和 SQL", exact=True).click()
    calculation = panel.get_by_test_id("stExpander").filter(has_text="核对统一口径的条款和 SQL")
    expect(calculation.get_by_text("绑定参数", exact=False).last).to_be_visible()
    panel.get_by_text("查看调查过程", exact=True).click()
    expect(panel.get_by_text("0 次模型调用", exact=False)).to_be_visible()

    question = panel.get_by_role("textbox", name="你想核对什么？")
    question.fill("华南电水壶十月的净营收是多少？")
    submit.click()
    expect(metrics).to_have_text(["¥292.80"], timeout=20000)
    expect(panel.get_by_text("问题中省略的查询年份按演示数据推断为 2026 年。", exact=True)).to_be_visible()

    question.fill("华南电水壶净营收差了多少？")
    submit.click()
    expect(panel.get_by_test_id("stAlert")).to_be_visible(timeout=20000)
    expect(metrics).to_have_count(0)

    panel.get_by_role("button", name="载入证据补查示例").click()
    expect(question).to_have_value(
        "华南电水壶 2026 年 11 月和 12 月的净营收差了多少？如果两个月都按 11 月口径重算，差额又是多少？"
    )
    expect(panel.get_by_role("combobox", name="调查方式")).to_have_value("自动 · 词面优先")
    submit.click()
    expect(metrics).to_have_text(["−¥186.40", "−¥190.40", "+¥4.00"], timeout=20000)
    investigation = panel.get_by_test_id("stExpander").filter(has_text="查看调查过程")
    if not investigation.get_by_text("按正文词面补查 · 已找到", exact=True).is_visible():
        investigation.get_by_text("查看调查过程", exact=True).click()
    expect(investigation.get_by_text("按正文词面补查 · 已找到", exact=True)).to_be_visible()
    expect(investigation.get_by_text("0 次模型调用", exact=False)).to_be_visible()
    print("PASS natural language policy comparison, cited counterfactual, clarification, and lexical recovery", flush=True)


def check_manual_investigation(page: Page, origin: str) -> None:
    page.goto(f"{origin}/?session={uuid4()}", wait_until="domcontentloaded")
    panel = page.get_by_role("tabpanel", name="自然语言调查")
    panel.get_by_text("手册问答", exact=True).click(timeout=20000)
    submit = panel.get_by_role("button", name="查阅手册")
    submit.click()
    expect(panel.get_by_text("顶部指示灯会保持绿色", exact=False)).to_be_visible(timeout=20000)
    panel.get_by_text("查看出处", exact=True).click()
    expect(panel.get_by_text("guide-v2.md · 第 2 版", exact=False)).to_be_visible()

    version = page.get_by_role("combobox", name="手册版本")
    control = panel.get_by_test_id("stSelectbox").filter(has=version)
    control.get_by_role("button", name="Open").click()
    page.get_by_role("option", name="第 1 版", exact=True).click()
    expect(version).to_have_value("第 1 版")
    submit.click()
    expect(panel.get_by_text("顶部指示灯会保持蓝色", exact=False)).to_be_visible(timeout=20000)

    page.get_by_role("tab", name="文档库").click()
    library = page.get_by_role("tabpanel", name="文档库")
    library.get_by_text("产品手册", exact=True).click()
    expect(library.get_by_role("radiogroup", name="文档类型")).to_have_count(0)
    sample = Path(__file__).resolve().parents[2] / "app" / "manual_corpus" / "guide-v1.md"
    library.get_by_test_id("stForm").first.locator('input[type="file"]').set_input_files(
        {"name": "browser-manual-v1.md", "mimeType": "text/markdown", "buffer": sample.read_bytes()},
    )
    expect(library.get_by_text("browser-manual-v1.md", exact=True)).to_be_visible(timeout=15000)
    library.get_by_role("button", name="上传文档", exact=True).click()
    expect(library.get_by_text("已上传第 1 版。", exact=True)).to_be_visible(timeout=20000)
    page.get_by_role("tab", name="自然语言调查").click()
    panel.get_by_text("手册问答", exact=True).click()
    panel.get_by_text("文档库手册", exact=True).click()
    expect(panel.get_by_role("combobox", name="手册", exact=True)).to_be_visible(timeout=15000)
    submit.click()
    expect(panel.get_by_text("顶部指示灯会保持蓝色", exact=False)).to_be_visible(timeout=20000)
    file = panel.get_by_text("browser-manual-v1.md · 第 1 版", exact=False)
    if not file.is_visible():
        panel.get_by_text("查看出处", exact=True).click()
    expect(file).to_be_visible()
    print("PASS manual Markdown upload, version-specific answers and cited source", flush=True)


def check_policy_exploration(page: Page, origin: str) -> None:
    page.goto(f"{origin}/?session={uuid4()}&workspace=policy-exploration", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="政策调查")).to_be_visible(timeout=20000)
    expect(page.get_by_role("radio", name="政策调查", exact=True)).to_have_count(1)
    expect(page.get_by_role("radio", name="政策问数", exact=True)).to_have_count(0)
    expect(page.get_by_role("radio", name="多源实验", exact=True)).to_have_count(0)
    page.get_by_role("tab", name="按条件查口径").click()
    panel = page.get_by_role("tabpanel", name="按条件查口径")

    def select(field: str, label: str) -> None:
        control = page.get_by_role("combobox", name=field)
        selectbox = panel.get_by_test_id("stSelectbox").filter(has=control)
        selectbox.scroll_into_view_if_needed()
        selectbox.get_by_role("button", name="Open").click()
        page.get_by_role("option", name=label, exact=True).click()
        expect(control).to_have_value(label)

    run = panel.get_by_role("button", name="运行政策探索")
    amount = panel.get_by_test_id("stMetricValue")
    expect(panel.get_by_role("combobox", name="执行方式")).to_have_value("自动 · 固定快路径 + 缓存计划 + 重规划")
    run.click()
    expect(amount).to_have_text("¥292.80", timeout=20000)
    panel.get_by_text("查看计划与执行", exact=True).click()
    expect(panel.get_by_text("实际路径 · 固定快路径", exact=True)).to_be_visible()
    expect(panel.get_by_text("0 次模型调用", exact=False)).to_be_visible()

    select("选择示例", "先找指定标题，再找当期公告")
    expect(panel.get_by_role("spinbutton", name="year, 起始日期")).to_have_text("2027")
    select("执行方式", "原固定流程 · 最多查一份公告")
    run.click()
    expect(panel.get_by_text("可切换到自动模式，或用确定性遍历继续核查多层引用。", exact=True)).to_be_visible()
    expect(amount).to_have_count(0)

    select("执行方式", "确定性遍历 · 支持多层公告")
    run.click()
    expect(amount).to_have_text("¥211.00", timeout=20000)
    panel.get_by_text("查看政策、公告和 SQL 证据", exact=True).click()
    expect(panel.get_by_text("第 2 层引用", exact=True)).to_be_visible()
    expect(panel.get_by_text("华南家电一月结算指引", exact=True)).to_be_visible()
    expect(panel.get_by_text("家电一月争议清算执行公告", exact=True)).to_be_visible()
    expect(panel.get_by_text("数据表 · 支付冲正（chargebacks）", exact=True)).to_be_visible()
    expect(panel.get_by_text("出处 app/hard_policy_corpus/bulletins.md", exact=False)).to_have_count(2)

    select("选择示例", "公告循环引用 · 检查拒答")
    expect(panel.get_by_role("textbox", name="产品")).to_have_value("蓝牙耳机")
    run.click()
    expect(panel.get_by_text("公告引用形成循环，无法确认最终口径。", exact=True)).to_be_visible()
    expect(amount).to_have_count(0)
    print("PASS cited multi-hop policy retrieval, original fixed-flow boundary, and cyclic-referral refusal", flush=True)


def check_policy_document_library(page: Page, origin: str) -> None:
    page.goto(f"{origin}/?session={uuid4()}&workspace=policy-exploration", wait_until="domcontentloaded")
    page.get_by_role("tab", name="文档库").click(timeout=20000)
    library = page.get_by_role("tabpanel", name="文档库")
    evidence = Path(__file__).resolve().parents[2] / "examples" / "demo"
    unique = str(uuid4())[:8]

    def upload(region, version: int) -> None:
        file = {"name": f"policy-{unique}-v{version}.md", "mimeType": "text/markdown",
                "buffer": (evidence / f"policy-upload-v{version}.md").read_bytes()}
        region.locator('input[type="file"]').set_input_files(file)

    upload_form = library.get_by_test_id("stForm").first
    upload(upload_form, 1)
    library.get_by_role("button", name="上传文档", exact=True).click()
    expect(library.get_by_text("已上传第 1 版。", exact=True)).to_be_visible(timeout=20000)
    expect(library.get_by_text(f"policy-{unique}-v1.md", exact=True).last).to_be_visible()
    expect(library.get_by_text("本机模式下文档仅此环境可见。", exact=False)).to_be_visible()

    def investigate(amount: str, version: int, *, choose_doc: bool) -> None:
        page.get_by_role("tab", name="自然语言调查").click()
        panel = page.get_by_role("tabpanel", name="自然语言调查")
        panel.get_by_text("文档库", exact=True).filter(visible=True).last.click()
        form = panel.get_by_test_id("stMultiSelect")
        expect(form).to_be_visible(timeout=20000)
        if choose_doc:
            form.get_by_role("button", name="Open").click()
            page.get_by_role("option", name=f"policy-{unique}-v{version}.md", exact=False).click()
            form.get_by_role("combobox").press("Escape")
        panel.get_by_role("textbox", name="你想核对什么？").fill("2026年10月华南电水壶净营收是多少？")
        panel.get_by_role("button", name="开始调查").click()
        expect(panel.get_by_test_id("stMetricValue")).to_have_text(amount, timeout=20000)
        evidence_panel = panel.get_by_test_id("stExpander").filter(has_text="核对实际口径的条款和 SQL")
        citation = evidence_panel.get_by_text(f"文档库 · 第 {version} 版", exact=True)
        if not citation.is_visible():
            evidence_panel.get_by_text("核对实际口径的条款和 SQL", exact=True).click()
        expect(citation).to_be_visible()
        expect(evidence_panel.get_by_text(f"policy-{unique}-v{version}.md", exact=False)).to_be_visible()

    investigate("¥292.80", 1, choose_doc=True)
    page.get_by_role("tab", name="文档库").click()
    library.get_by_text("上传新版本", exact=True).click()
    upload(library.get_by_test_id("stForm").last, 2)
    library.get_by_role("button", name="保存新版本").click()
    expect(library.get_by_text("已保存第 2 版。", exact=True)).to_be_visible(timeout=20000)
    expect(library.get_by_role("combobox", name="查看版本")).to_have_value("第 2 版")
    version = library.get_by_role("combobox", name="查看版本")
    expect(library.get_by_text(f"policy-{unique}-v2.md", exact=True).last).to_be_visible()
    version.scroll_into_view_if_needed()
    version.press("ArrowDown")
    version.press("End")
    version.press("Enter")
    expect(version).to_have_value("第 1 版")
    expect(library.get_by_text(f"policy-{unique}-v1.md", exact=True).last).to_be_visible()
    investigate("¥294.80", 2, choose_doc=False)
    print("PASS Markdown upload, history, and cited evidence follows the new revision", flush=True)


def main():
    session = uuid4()
    origin = os.getenv("RAG_TEST_UI_URL", "http://127.0.0.1:8501")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
        try:
            exploration = browser.new_page(viewport={"width": 1280, "height": 900})
            check_policy_investigation(exploration, origin)
            check_manual_investigation(exploration, origin)
            check_policy_exploration(exploration, origin)
            check_policy_document_library(exploration, origin)
            exploration.close()

            policy = browser.new_page(viewport={"width": 1280, "height": 900})
            policy.goto(f"{origin}/?session={uuid4()}&workspace=policy", wait_until="domcontentloaded")
            expect(policy.get_by_role("heading", name="政策调查")).to_be_visible(timeout=20000)
            expect(policy.get_by_role("tab", name="日期分段")).to_have_attribute("aria-selected", "true")
            policy.get_by_role("button", name="按政策计算净营收").click()
            expect(policy.get_by_text("¥477.00", exact=True)).to_be_visible(timeout=20000)
            first = policy.get_by_text("2026-10-01 至 2026-10-15 · ¥350.00", exact=True)
            second = policy.get_by_text("2026-10-15 至 2026-11-01 · ¥127.00", exact=True)
            first.click()
            expect(policy.get_by_text("退款政策 · 2026 年 10 月上半月版", exact=False)).to_be_visible()
            second.click()
            expect(policy.get_by_text("退款政策 · 2026 年 10 月修订版", exact=False)).to_be_visible()
            expect(policy.get_by_text("绑定参数 [300,", exact=False)).to_be_visible()
            policy.reload(wait_until="domcontentloaded")
            expect(policy.get_by_role("tab", name="日期分段")).to_have_attribute("aria-selected", "true", timeout=20000)
            policy.goto(f"{origin}/?session={uuid4()}&workspace=multisource", wait_until="domcontentloaded")
            multi = policy.get_by_role("tabpanel", name="多源实验")
            expect(multi.get_by_role("button", name="运行多源问数")).to_be_visible(timeout=20000)
            policy.close()
            print("PASS canonical policy entry and preserved classic and multisource deep links", flush=True)

            starter = browser.new_page(viewport={"width": 1280, "height": 900})
            starter.goto(f"{origin}/?session={uuid4()}&workspace=chat", wait_until="domcontentloaded")
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
            page.goto(f"{origin}/?session={session}&workspace=chat", wait_until="domcontentloaded")
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
