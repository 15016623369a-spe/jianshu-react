"""Streamlit workspace for auditable policy and ledger exploration."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
import streamlit as st

_MODES = {
    "auto": "自动 · 固定快路径 + 缓存计划 + 重规划",
    "fixed": "原固定流程 · 最多查一份公告",
    "uniform": "确定性遍历 · 支持多层公告",
    "plan": "Plan-and-Execute · 缓存优先",
    "uncached": "Plan-and-Execute · 每次生成计划",
}
_REGIONS = ("华南", "华东", "华北")
_EXAMPLES = {
    "按生效日期找最新公告 · 10 月": ("电水壶", "华南", date(2026, 10, 1), date(2026, 10, 31)),
    "按生效日期找最新公告 · 11 月": ("电水壶", "华南", date(2026, 11, 1), date(2026, 11, 30)),
    "按自然语言标题找公告": ("帐篷", "华北", date(2026, 10, 1), date(2026, 10, 31)),
    "产品匹配类目政策": ("帐篷", "华南", date(2026, 10, 1), date(2026, 10, 31)),
    "精确产品政策覆盖类目": ("空气炸锅", "华东", date(2026, 10, 1), date(2026, 10, 31)),
    "只有相似说明 · 检查拒答": ("清仓手表", "华东", date(2026, 11, 1), date(2026, 11, 30)),
    "先找指定标题，再找当期公告": ("电水壶", "华南", date(2027, 1, 1), date(2027, 1, 31)),
    "先找当期公告，再跟进指定标题": ("帐篷", "华北", date(2027, 1, 1), date(2027, 1, 31)),
    "连续核查三层公告引用": ("空气炸锅", "华东", date(2027, 1, 1), date(2027, 1, 31)),
    "公告循环引用 · 检查拒答": ("蓝牙耳机", "华东", date(2027, 1, 1), date(2027, 1, 31)),
}
_TOOLS = {
    "plan": "生成执行计划",
    "replan": "调整执行计划",
    "cache_hit": "复用已核验计划",
    "cache_miss": "未找到适用计划",
    "cache_invalidated": "原计划已失效",
    "fixed_route": "执行确定性流程",
    "identify_product": "查找产品类目",
    "find_policy": "检索并核对适用政策",
    "find_latest_bulletin": "按生效日期找最新公告",
    "find_titled_bulletin": "按自然语言标题找公告",
    "follow_references": "继续核查剩余公告引用",
    "inspect_ledger": "检查适用账本结构",
    "query": "执行只读 SQL",
    "verify": "核验结果",
}
_STATUSES = {
    "ok": "完成", "error": "失败", "blocked": "已拦截",
    "invalid": "无效", "duplicate": "重复动作已拦截", "skipped": "跳过",
}
_TRACE_REASONS = {
    "document_version_changed": "文档版本已变化",
    "evidence_path_changed": "已核实的引用路径发生变化",
}
_EVIDENCE = {"category": "产品类目", "policy": "适用政策", "bulletin": "公告", "schema": "账本结构", "query": "只读查询"}
_LEDGERS = {"orders": "主订单", "refunds": "售后退款", "chargebacks": "支付冲正"}
_ROUTES = {
    "fixed_fast": "固定快路径",
    "cache": "复用缓存计划",
    "planned": "生成新计划",
    "replan": "根据新证据重规划",
    "uniform": "确定性遍历",
    "fixed": "原固定流程",
}


def _yuan(cents: int) -> str:
    return f"¥{Decimal(cents) / 100:,.2f}"


def _render_document(document: dict) -> None:
    title = document.get("title") or document.get("id")
    if title:
        st.text(title)
    if quote := document.get("quote"):
        st.text(quote)
    if document.get("start") and document.get("end_exclusive"):
        st.caption(f"{document['start']} 至 {document['end_exclusive']}（截止日不含）")
    if source := document.get("file"):
        first_line = document.get("line_start", document.get("line"))
        last_line = document.get("line_end", document.get("end_line"))
        span = f" · 第 {first_line}–{last_line} 行" if first_line and last_line else ""
        st.text(f"出处 {source}{span}")
    if document.get("title") and document.get("id"):
        st.caption(document["id"])


def _render_evidence(evidence: dict) -> None:
    if not any(evidence.get(name) for name in _EVIDENCE) and not evidence.get("bulletins"):
        return
    with st.expander("查看政策、公告和 SQL 证据"):
        for source, label in _EVIDENCE.items():
            value = evidence.get(source)
            if source == "bulletin" and (bulletins := evidence.get("bulletins")):
                st.markdown("**公告引用链**")
                for index, bulletin in enumerate(bulletins, 1):
                    st.caption(f"第 {index} 层引用")
                    _render_document(bulletin)
                continue
            if value is None:
                continue
            st.markdown(f"**{label}**")
            if source in ("policy", "bulletin") and isinstance(value, dict):
                _render_document(value)
            elif source == "query" and isinstance(value, dict) and value.get("sql"):
                if table := value.get("table") or value.get("ledger"):
                    st.caption(f"数据表 · {_LEDGERS.get(table, table)}（{table}）")
                st.code(value["sql"], language="sql")
                st.text("绑定参数 " + json.dumps(value.get("parameters", []), ensure_ascii=False))
            elif isinstance(value, str):
                st.text(value)
            else:
                st.json(value)


def _render_result(result: dict) -> None:
    evidence = result.get("evidence", {})
    if result["kind"] == "answer":
        st.metric("政策口径净营收", _yuan(result["total_cents"]))
        scope = result["scope"]
        st.caption(f"{scope['product']} · {scope['region']} · {scope['start']} 至 {scope['end_exclusive']}（截止日不含）")
        query = evidence["query"]
        st.caption(f"{query['order_count']} 笔订单 · 毛营收 {_yuan(query['gross_cents'])} · 政策扣减 {_yuan(query['deducted_cents'])}")
    elif result["kind"] == "clarification":
        st.info(result["message"])
    else:
        st.warning(result["message"])
        if result.get("reason") == "workflow_unsupported":
            st.caption("可切换到自动模式，或用确定性遍历继续核查多层引用。")

    _render_evidence(evidence)
    metrics = result["metrics"]
    with st.expander("查看计划与执行"):
        st.caption(_MODES[result["mode"]])
        if result.get("route") in _ROUTES:
            st.caption(f"实际路径 · {_ROUTES[result['route']]}")
        if version := result.get("document_version") or evidence.get("document_version"):
            st.caption("文档版本指纹")
            st.code(str(version), language=None)
        st.caption(f"{metrics['latency_ms']:.1f} ms · {metrics['sql_queries']} 次 SQL 查询 · "
                   f"{metrics['model_calls']} 次模型调用")
        if result["mode"] in ("auto", "plan", "uncached"):
            st.caption(f"计划缓存命中 {metrics.get('cache_hits', 0)} 次 · 重规划 {metrics.get('replans', 0)} 次")
            if count := metrics.get("cache_invalidated", 0):
                st.caption(f"原计划失效 {count} 次")
        if metrics["model_calls"]:
            st.caption(f"{metrics['total_tokens']:,} token" if metrics.get("usage_complete", True) else "模型用量记录不完整。")
        for event in result["tool_trace"]:
            label = _TOOLS.get(event["tool"], event["tool"] or "模型调用")
            status = _STATUSES.get(event["status"], event["status"])
            reason = event.get("reason")
            detail = f" · {_TRACE_REASONS.get(reason, reason)}" if reason else ""
            st.text(f"{event['step']}. {label} · {status}{detail}")
            if ids := event.get("ids"):
                separator = " → " if event["tool"] in ("plan", "replan", "cache_hit") else "、"
                st.caption(separator.join(_TOOLS.get(value, value) for value in ids))


def render_hard_policy_workspace(api: str, headers: dict[str, str], session_id: str, model_configured: bool) -> None:
    st.caption("检索 Markdown 政策正文，跟进公告引用，再按核实的账本计算。演示订单和文档均为合成数据。")
    fields = tuple(f"policy-exploration-{name}-{session_id}" for name in ("product", "region", "start", "end"))
    example_key = f"policy-exploration-example-{session_id}"

    def load_example():
        for key, value in zip(fields, _EXAMPLES[st.session_state[example_key]], strict=True):
            st.session_state[key] = value
        st.session_state.pop("hard_policy_result", None)

    st.selectbox("选择示例", list(_EXAMPLES), key=example_key, on_change=load_example)
    if fields[0] not in st.session_state:
        load_example()
    product_key, region_key, start_key, end_key = fields
    first_date, last_date = date(2000, 1, 1), date(2099, 12, 31)
    with st.form(f"hard-policy-{session_id}"):
        modes = list(_MODES) if model_configured else ["auto", "fixed", "uniform", "plan"]
        mode = st.selectbox("执行方式", modes, format_func=_MODES.__getitem__)
        if model_configured:
            st.caption("自动模式优先处理可确定的短路径，再复用当前文档版本的计划。证据推翻已有计划时会重新规划。")
        else:
            st.caption("当前可使用自动模式、确定性流程和已缓存计划。实时规划需要模型配置。")
        product_column, region_column = st.columns(2)
        product = product_column.text_input("产品", max_chars=32, key=product_key)
        region = region_column.selectbox("地区", _REGIONS, key=region_key)
        start_column, end_column = st.columns(2)
        start = start_column.date_input("起始日期", min_value=first_date, max_value=last_date, key=start_key)
        end = end_column.date_input("结束日期（含当天）", min_value=first_date, max_value=last_date, key=end_key)
        submitted = st.form_submit_button("运行政策探索", type="primary")

    result_key = "hard_policy_result"
    if submitted:
        st.session_state.pop(result_key, None)
        with st.spinner("正在检查政策、公告与账本"):
            try:
                response = httpx.post(
                    api + "/api/v2/policy-exploration", headers=headers, params={"mode": mode}, json={
                        "product": product.strip() or None,
                        "region": region,
                        "start": start.isoformat() if start else None,
                        "end_exclusive": (end + timedelta(days=1)).isoformat() if end else None,
                    },
                    timeout=45, trust_env=False,
                )
                response.raise_for_status()
                st.session_state[result_key] = (session_id, response.json())
            except (httpx.HTTPError, ValueError):
                st.error("暂时无法完成政策探索，请检查请求或稍后重试。")
    saved_session, result = st.session_state.get(result_key, (None, None))
    if saved_session == session_id and result:
        _render_result(result)
