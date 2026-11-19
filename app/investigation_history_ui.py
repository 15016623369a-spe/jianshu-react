"""History and export controls for durable investigations."""
from __future__ import annotations

import json
from datetime import datetime

import httpx
import streamlit as st


def _get(api: str, headers: dict[str, str], path: str) -> httpx.Response:
    return httpx.get(api + path, headers=headers, timeout=15, trust_env=False)


def _download(api: str, headers: dict[str, str], path: str, label: str, filename: str) -> None:
    try:
        response = _get(api, headers, path)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError):
        st.warning(f"{label}暂时不可用。")
        return
    st.download_button(label, response.content, file_name=filename, mime=response.headers.get("content-type", "application/octet-stream"))


def render_investigation_history(api: str, headers: dict[str, str], session_id: str) -> None:
    st.caption("每次调查都保存了当时的问题、范围、金额、证据和执行摘要。打开历史不会用今天的数据重新计算。")
    try:
        page = _get(api, headers, "/api/v3/investigations?limit=30").json()
    except (httpx.HTTPError, ValueError):
        st.error("暂时无法读取调查历史。")
        return
    records = page.get("investigations", [])
    if not records:
        st.info("还没有调查记录。先在「自然语言调查」发起一次调查。")
        return
    labels = {
        item["investigation_id"]: f"{item['question']} · {item['status']} · {item['created_at'][:16]}"
        for item in records
    }
    selected = st.selectbox("选择调查记录", list(labels), format_func=labels.__getitem__,
                            key=f"history-record-{session_id}")
    record = next(item for item in records if item["investigation_id"] == selected)
    st.subheader(record["question"])
    st.caption(f"{record['domain']} · {record['status']} · {record['created_at']} · {record['investigation_id']}")
    if record.get("kind") == "answer":
        if record.get("single_period_delta_cents") is not None:
            actual, unified = record["actual"][0], record["counterfactual"][0]
            columns = st.columns(3)
            columns[0].metric("实际净营收", f"¥{actual['net_cents'] / 100:,.2f}")
            columns[1].metric("统一口径净营收", f"¥{unified['net_cents'] / 100:,.2f}")
            columns[2].metric("实际减统一", f"¥{record['single_period_delta_cents'] / 100:,.2f}")
        elif record.get("actual_delta_cents") is not None:
            columns = st.columns(3)
            columns[0].metric("实际差额", f"¥{record['actual_delta_cents'] / 100:,.2f}")
            columns[1].metric("统一口径差额", f"¥{record['counterfactual_delta_cents'] / 100:,.2f}")
            columns[2].metric("口径影响", f"¥{record['policy_effect_cents'] / 100:,.2f}")
        else:
            st.write(record.get("answer") or record.get("message") or "")
        if record.get("answer"):
            st.markdown(record["answer"])
        if record.get("evidence"):
            with st.expander("查看证据"):
                for item in record["evidence"]:
                    st.markdown(f"**[{item['source']}] {item['title']}** · `{item['file']}:{item['line_start']}` · 第 {item['version']} 版")
                    st.caption(item["quote"])
    else:
        st.info(record.get("message") or "调查未完成")
        if reason := record.get("reason"):
            st.caption(f"原因：{reason}")
    with st.expander("执行摘要"):
        metrics = record.get("metrics") or {}
        st.caption(
            f"SQL {metrics.get('sql_queries', 0)} 次 · 检索 {metrics.get('retrievals', 0)} 次 · "
            f"模型 {metrics.get('model_calls', 0)} 次 · {metrics.get('latency_ms', 0)} ms"
        )
        st.caption(f"文档版本：{record.get('document_version') or '—'} · 数据版本：{record.get('data_version') or '—'}")
    with st.expander("导出"):
        _download(api, headers, f"/api/v3/investigations/{selected}/report.md", "下载 Markdown 调查报告",
                  f"investigation-{selected}.md")
        _download(api, headers, f"/api/v3/investigations/{selected}/amounts.csv", "下载金额明细 CSV",
                  f"investigation-{selected}.csv")
