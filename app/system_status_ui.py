"""Read-only system capability and dependency status."""
from __future__ import annotations

import httpx
import streamlit as st


def render_system_status(api: str, headers: dict[str, str]) -> None:
    try:
        status = httpx.get(api + "/api/v3/status", headers=headers, timeout=10, trust_env=False).json()
    except (httpx.HTTPError, ValueError):
        st.error("暂时无法读取系统状态。")
        return
    st.caption("这里显示当前可用的领域、数据源和模型。模型未配置时，确定性检索路径仍可使用。")
    columns = st.columns(3)
    columns[0].metric("模型", "已配置" if status.get("model_configured") else "未配置")
    columns[1].metric("政策数据", "可用" if status.get("data_sources", {}).get("policy_database") else "不可用")
    columns[2].metric("历史记录", "可用" if status.get("history_available") else "不可用")
    st.markdown("### 领域")
    st.json(status.get("domains", {}))
    st.markdown("### 数据与索引")
    st.json(status.get("data_sources", {}))
    st.caption(f"检索：{status.get('retrieval', '—')}")
    st.caption("说明：模型不可用时，政策/手册的确定性路径仍会执行；只有模型补查会失败并返回可读原因。")
