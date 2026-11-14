"""Streamlit workspace for the opt-in multi-source workflow comparison."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
import streamlit as st

_MODES = {"fixed": "固定流程", "react": "ReAct 实验"}
_TOOLS = {
    "discover_groups": "在订单库发现产品和地区",
    "lookup_policies": "查找适用政策",
    "lookup_bulletins": "查找引用的补充公告",
    "aggregate_groups": "回订单库分组汇总",
    "finish": "提交证据核验",
}
_STATUSES = {
    "ok": "完成", "error": "执行失败", "blocked": "已拦截",
    "invalid": "动作无效", "duplicate": "重复动作已拦截",
}
_SOURCES = {"policy": "政策", "policies": "政策", "bulletin": "补充公告", "bulletins": "补充公告"}


def _yuan(cents: int) -> str:
    return f"¥{Decimal(cents) / 100:,.2f}"


def _render_evidence(evidence: dict) -> None:
    if not any(evidence.values()):
        return
    with st.expander("查看来源证据"):
        discovery = evidence.get("discovery", [])
        if discovery:
            st.markdown("**从订单库发现的范围**")
            st.dataframe([{
                "分组": group["id"], "产品": group["product"], "地区": group["region"],
                "退款订单数": group["order_count"],
            } for group in discovery], hide_index=True, width="stretch")

        for document in evidence.get("documents", []):
            source = _SOURCES.get(document["source"], document["source"])
            st.text(f"{source} · {document['id']}")
            st.text(document["quote"])
            st.text(f"{document['start']} 至 {document['end_exclusive']}（不含结束日）"
                    + (f" · 引用 {document['ref']}" if document.get("ref") else ""))

        for query in evidence.get("queries", []):
            st.text(f"只读 SQL · {query['id']} · {query['group_id']}")
            st.code(query["sql"], language="sql")
            st.text("绑定参数 " + json.dumps(query["parameters"], ensure_ascii=False))


def _render_result(result: dict) -> None:
    if result["kind"] == "answer":
        scope = result["scope"]
        st.metric("发生退款的分组净营收", _yuan(result["total_cents"]))
        st.caption(f"{scope['start']} 至 {scope['end_exclusive']}（不含结束日） · {scope['region'] or '全部地区'}")
        if result["groups"]:
            st.subheader("产品与地区明细")
            st.dataframe([{
                "产品": group["product"], "地区": group["region"], "分组订单数": group["order_count"],
                "订单金额": _yuan(group["gross_cents"]), "政策扣减": _yuan(group["deducted_cents"]),
                "净营收": _yuan(group["net_cents"]), "来源": "、".join(group["evidence_ids"]),
            } for group in result["groups"]], hide_index=True, width="stretch")
        else:
            st.info("所选范围内没有退款订单。")
        _render_evidence(result["evidence"])
    elif result["kind"] == "clarification":
        st.info(result["message"])
    else:
        st.warning(result["message"])

    metrics = result["metrics"]
    with st.expander("查看执行步骤"):
        st.caption(_MODES[result["mode"]])
        st.caption(f"{metrics['latency_ms']:.1f} ms · {metrics['sql_queries']} 次 SQL 查询 · "
                   f"{metrics['model_calls']} 次模型调用")
        if metrics["model_calls"]:
            if metrics.get("usage_complete", True):
                st.caption(f"{metrics['total_tokens']:,} token")
            else:
                st.caption("模型用量记录不完整。")
        for event in result["tool_trace"]:
            label = _TOOLS.get(event["tool"], "模型动作")
            status = _STATUSES.get(event["status"], event["status"])
            ids = " · " + "、".join(event["ids"]) if event["ids"] else ""
            reason = f" · {event['reason']}" if event.get("reason") else ""
            st.text(f"{event['step']}. {label} · {status}{ids}{reason}")


def render_multisource_workspace(api: str, headers: dict[str, str], session_id: str, model_configured: bool) -> None:
    st.caption("实验使用合成数据。先识别发生退款的产品与地区，再统计这些分组在所选日期内的全部订单。")
    with st.form(f"multisource-{session_id}"):
        mode = st.radio("执行方式", list(_MODES) if model_configured else ["fixed"],
                        format_func=_MODES.__getitem__, horizontal=True)
        if not model_configured:
            st.caption("配置模型后可选择 ReAct 实验。")
        left, middle, right = st.columns(3)
        start = left.date_input("起始日期", date(2026, 10, 1), min_value=date(2000, 1, 1),
                                max_value=date(2099, 12, 31))
        end = middle.date_input("结束日期（含当天）", date(2026, 10, 12), min_value=date(2000, 1, 1),
                                max_value=date(2099, 12, 31))
        region = right.selectbox("地区", (None, "华南", "华东", "华北"),
                                 format_func=lambda item: item or "全部地区")
        submitted = st.form_submit_button("运行多源问数", type="primary")

    result_key = "multisource_result"
    if submitted:
        st.session_state.pop(result_key, None)
        try:
            with st.spinner("正在查找订单和适用政策"):
                response = httpx.post(
                    api + "/api/v2/multisource", headers=headers, params={"mode": mode},
                    json={
                        "start": start.isoformat() if start is not None else None,
                        "end_exclusive": (end + timedelta(days=1)).isoformat() if end is not None else None,
                        "region": region,
                    },
                    timeout=45, trust_env=False,
                )
                response.raise_for_status()
                st.session_state[result_key] = (session_id, response.json())
        except (httpx.HTTPError, ValueError):
            st.error("暂时无法完成多源问数，请检查日期范围或稍后重试。")
    saved_session, result = st.session_state.get(result_key, (None, None))
    if saved_session == session_id and result:
        _render_result(result)
