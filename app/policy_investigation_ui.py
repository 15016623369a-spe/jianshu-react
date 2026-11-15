"""Natural-language policy comparison in the policy exploration workspace."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal

import httpx
import streamlit as st

from app.hard_policy_ui import _LEDGERS, _render_document
from app.policy_library_ui import document_label

_FLAGSHIP_QUESTION = (
    "华南电水壶 2026 年 10 月和 11 月的净营收差了多少？"
    "如果两个月都按 10 月口径重算，差额又是多少？"
)
_RECOVERY_QUESTION = (
    "华南电水壶 2026 年 11 月和 12 月的净营收差了多少？"
    "如果两个月都按 11 月口径重算，差额又是多少？"
)
_MODES = {
    "auto": "自动 · 词面优先",
    "lexical": "确定性检索 + 词面补查",
    "fixed": "确定性多跳检索",
    "model": "模型补查实验",
}
_FILE_VERSION = re.compile(r"^uploads/[a-f0-9-]+/v([1-9][0-9]*)/")
_STAGES = {
    "interpret": "解析查询条件",
    "scope": "确认查询条件",
    "resolve": "核对适用政策",
    "retrieve": "检索政策与公告",
    "retrieve_policy": "检索适用政策",
    "retrieve_bulletin": "检索适用公告",
    "retrieve_rewritten": "寻找补充证据",
    "retrieve_lexical": "按正文词面补查",
    "retrieve_model": "用模型生成的词补查",
    "expand": "按正文词面补查",
    "inspect_ledger": "核对账本字段",
    "rewrite": "按证据缺口调整检索",
    "query": "执行只读查询",
    "execute_sql": "执行只读查询",
    "verify": "核对金额与证据",
    "stopped": "调查已结束",
}
_STATUSES = {
    "ok": "完成",
    "gap": "缺少证据",
    "miss": "未找到",
    "found": "已找到",
    "clarification": "待确认",
    "abstained": "停止",
    "error": "失败",
    "skipped": "跳过",
    "rejected": "未采用",
}


def _format_money(cents: int, *, signed: bool = False) -> str:
    sign = "−" if cents < 0 else "+" if signed and cents > 0 else ""
    return f"{sign}¥{Decimal(abs(cents)) / 100:,.2f}"


def _period_label(period: dict) -> str:
    start = date.fromisoformat(period["start"])
    end = date.fromisoformat(period["end_exclusive"]) - timedelta(days=1)
    if start == end:
        return start.isoformat()
    if start.day == 1 and start.month == end.month and (end + timedelta(days=1)).day == 1:
        return f"{start.year} 年 {start.month} 月"
    return f"{start.isoformat()} 至 {end.isoformat()}"


def _rule_label(rule: dict) -> str:
    ledger, cap = rule["ledger"], rule["cap_cents"]
    cap_label = "逐笔扣除退款" if cap is None else f"每单扣减上限 {_format_money(cap)}"
    return f"{_LEDGERS.get(ledger, ledger)} · {cap_label}"


def _render_calculation(entries: list[dict], label: str) -> None:
    with st.expander(label):
        for entry in entries:
            st.markdown(f"**{_period_label(entry['period'])}**")
            for segment in entry["segments"]:
                query = segment["query"]
                st.caption(f"{_period_label(segment['period'])} · {_rule_label(segment['rule'])}")
                st.caption(
                    f"{query['order_count']} 笔订单 · 毛营收 {_format_money(query['gross_cents'])} · "
                    f"扣减 {_format_money(query['deducted_cents'])} · 净营收 {_format_money(query['net_cents'])}"
                )
                for document in segment["documents"]:
                    _render_document(document)
                    if revision := _FILE_VERSION.match(document.get("file", "")):
                        st.caption(f"文档库 · 第 {revision[1]} 版")
                st.code(query["sql"], language="sql")
                st.text("绑定参数 " + json.dumps(query["parameters"], ensure_ascii=False))


def _render_answer(result: dict) -> None:
    scope = result["scope"]
    periods, counterfactual = scope["periods"], result["counterfactual"]
    timeframe = _period_label(periods[0]) if len(periods) == 1 else (
        f"{_period_label(periods[1])} 减 {_period_label(periods[0])}"
    )
    st.caption(f"{scope['region']} · {scope['product']} · {timeframe}")
    if year := scope.get("assumed_year"):
        st.caption(f"问题中省略的查询年份按演示数据推断为 {year} 年。")
    if len(periods) == 1:
        st.metric("实际净营收", _format_money(result["actual"][0]["net_cents"]))
    else:
        metric_cards = st.columns(3) if counterfactual else [st.container()]
        metric_cards[0].metric("实际口径差额", _format_money(result["actual_delta_cents"], signed=True))
        if counterfactual:
            metric_cards[1].metric("统一口径差额", _format_money(result["counterfactual_delta_cents"], signed=True))
            metric_cards[2].metric("口径差额影响", _format_money(result["policy_effect_cents"], signed=True))

    rows = []
    for index, month in enumerate(result["actual"]):
        row = {
            "期间": _period_label(month["period"]),
            "订单": f"{month['order_count']} 笔",
            "实际净营收": _format_money(month["net_cents"]),
        }
        if counterfactual:
            row["统一口径净营收"] = _format_money(counterfactual[index]["net_cents"])
        rows.append(row)
    st.table(rows, hide_index=True, border="horizontal")
    for month in result["actual"]:
        for segment in month["segments"]:
            st.caption(f"实际口径 · {_period_label(segment['period'])} · {_rule_label(segment['rule'])}")

    if counterfactual:
        base_label = _period_label(periods[scope["baseline_period"]])
        st.caption(f"统一口径沿用 {base_label}的退款规则重算同一批订单。口径差额影响等于实际差额减去统一口径差额。")
    _render_calculation(result["actual"], "核对实际口径的条款和 SQL")
    if counterfactual:
        _render_calculation(counterfactual, "核对统一口径的条款和 SQL")


def _render_trace(result: dict) -> None:
    metrics = result.get("metrics")
    if not metrics:
        return
    with st.expander("查看调查过程"):
        st.caption(
            f"{metrics['latency_ms']:,.0f} ms · {metrics['retrievals']} 次检索 · "
            f"{metrics['rewrites']} 次改查 · {metrics['sql_queries']} 次 SQL · {metrics['model_calls']} 次模型调用"
        )
        if metrics.get("lexical_hits"):
            st.caption(f"词面补查命中 {metrics['lexical_hits']} 次")
        if metrics.get("model_fallbacks"):
            st.caption(f"模型兜底 {metrics['model_fallbacks']} 次")
        if metrics["model_calls"]:
            tokens = f"{metrics['total_tokens']:,} token"
            st.caption(tokens if metrics.get("usage_complete", True) else f"已记录 {tokens} · 用量统计不完整")
        for event in result.get("tool_trace", []):
            stage = _STAGES.get(event["stage"], event["stage"])
            status = _STATUSES.get(event["status"], event["status"])
            st.text(f"{stage} · {status}")
            if detail := event.get("detail"):
                st.text(detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
        if version := result.get("document_version"):
            st.caption("文档版本")
            st.code(version, language=None)


def render_policy_investigation(
    api: str, headers: dict[str, str], session_id: str, model_configured: bool, registry: dict | None,
) -> None:
    st.caption("查询一个月或比较两个月的净营收，可指定统一退款口径。演示问题省略年份时使用 2026 年。")
    result_key = "policy-investigation-result"
    question_key = f"investigation-question-{session_id}"
    source_key = f"investigation-doc-source-{session_id}"
    documents_key = f"investigation-documents-{session_id}"
    st.session_state.setdefault(question_key, _FLAGSHIP_QUESTION)

    def load_example(question: str) -> None:
        st.session_state[question_key] = question
        st.session_state[source_key] = "演示政策"
        st.session_state.pop(result_key, None)

    with st.container(horizontal=True):
        st.button("载入口径对比示例", on_click=load_example, args=(_FLAGSHIP_QUESTION,))
        st.button("载入证据补查示例", on_click=load_example, args=(_RECOVERY_QUESTION,))

    docs = {document["id"]: document for document in registry["documents"]} if registry is not None else {}
    use_library = st.radio("资料来源", ["演示政策", "文档库"], horizontal=True, key=source_key,
                           persist_state="session") == "文档库"
    selected = []
    if use_library:
        if registry is None:
            st.warning("暂时无法读取文档库。")
        elif docs:
            previous = st.session_state.get(documents_key, [])
            available = [doc_id for doc_id in previous if doc_id in docs]
            if available != previous:
                st.session_state[documents_key] = available
            selected = st.multiselect(
                "选择调查文档", list(docs), format_func=lambda key: document_label(docs[key]),
                max_selections=8, key=documents_key, persist_state="session",
            )
            st.caption("使用所选文档的最新可读版本。文档仅在本地检索，金额按演示订单计算。")
        else:
            st.info("请先在「文档库」上传文档，或请文档所有者授予读取权限。")

    evidence_scope = tuple(sorted((doc_id, docs[doc_id]["version"]) for doc_id in selected))
    visible_scope = session_id, use_library, evidence_scope

    mode_key = f"investigation-mode-{session_id}"
    modes = list(_MODES) if model_configured and not use_library else ["auto", "lexical", "fixed"]
    if st.session_state.get(mode_key, "auto") not in modes:
        st.session_state[mode_key] = "auto"
    with st.form(f"policy-investigation-{session_id}"):
        question = st.text_area("你想核对什么？", key=question_key, max_chars=500, height=102,
                                persist_state="session")
        mode = st.selectbox("调查方式", modes, format_func=_MODES.__getitem__, key=mode_key,
                            persist_state="session")
        if model_configured and not use_library:
            st.caption("自动模式先用确定性检索。补查仍无结果时，相关文档片段可能用于模型改写。")
        submitted = st.form_submit_button("开始调查", type="primary", disabled=bool(use_library and not selected))

    if submitted:
        st.session_state.pop(result_key, None)
        if not question.strip():
            st.info("请输入要核对的问题。")
            return
        with st.spinner("正在检查适用规则和账本"):
            try:
                response = httpx.post(
                    api + "/api/v2/policy-investigation", headers=headers, params={"mode": mode},
                    json={"question": question.strip(), "document_ids": selected}, timeout=45, trust_env=False,
                )
                response.raise_for_status()
                st.session_state[result_key] = visible_scope, response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404 and use_library:
                    st.warning("所选文档已不可用，请重新选择。")
                else:
                    st.error("暂时无法完成调查，请稍后重试。")
            except (httpx.HTTPError, ValueError):
                st.error("暂时无法完成调查，请稍后重试。")

    saved_scope, result = st.session_state.get(result_key, (None, None))
    if saved_scope != visible_scope or not result or (use_library and not selected):
        return
    if result["kind"] == "answer":
        _render_answer(result)
    elif result["kind"] == "clarification":
        st.info(result["message"])
    else:
        st.warning(result["message"])
    _render_trace(result)
