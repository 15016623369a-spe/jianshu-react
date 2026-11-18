"""Retail metric investigation in the shared policy workbench."""
from __future__ import annotations

import json

import httpx
import streamlit as st

_EXAMPLE = "2026 年 10 月华南电水壶净营收是多少？"


def render_retail_investigation(api: str, headers: dict[str, str], session_id: str) -> None:
    st.caption("确定性 SQL：按商品、地区、月份查询订单、退款、客单价与净营收。")
    result_key = f"retail-result-{session_id}"
    question_key = f"retail-question-{session_id}"
    st.session_state.setdefault(question_key, _EXAMPLE)
    with st.form(f"retail-form-{session_id}"):
        question = st.text_area("你想核对什么？", key=question_key, max_chars=500, height=90)
        submitted = st.form_submit_button("查询零售指标", type="primary")
    if submitted:
        st.session_state.pop(result_key, None)
        if not question.strip():
            st.info("请输入商品、地区、月份和指标。")
        else:
            try:
                response = httpx.post(
                    api + "/api/v3/investigations", headers=headers,
                    json={"domain": "retail", "question": question.strip()}, timeout=30, trust_env=False,
                )
                response.raise_for_status()
                st.session_state[result_key] = response.json()
            except (httpx.HTTPError, ValueError):
                st.error("零售查询暂时不可用，请稍后重试。")
    result = st.session_state.get(result_key)
    if not result:
        return
    if result["kind"] == "answer":
        scope = result.get("scope") or {}
        st.caption(f"{scope.get('region')} · {scope.get('product')} · {scope.get('metric')}")
        st.metric("结果", result.get("answer", "—"))
        st.table([{
            "订单": f"{result['actual'][0]['order_count']} 笔",
            "毛营收": f"¥{result['actual'][0]['gross_cents'] / 100:,.2f}",
            "退款": f"¥{result['actual'][0]['refund_cents'] / 100:,.2f}",
            "净营收": f"¥{result['actual'][0]['net_cents'] / 100:,.2f}",
        }])
        for item in result.get("evidence", []):
            with st.expander("查看执行证据"):
                st.caption(f"来源：{item.get('title')} · {item.get('file')}")
                st.code(item.get("quote", ""), language="sql")
    else:
        st.info(result.get("message") or "未完成")
    st.caption(json.dumps(result.get("metrics", {}), ensure_ascii=False))
