"""demo dashboard: each button runs one step from demo/actions.py on the live gateway.

  uv run --group demo streamlit run demo/dashboard.py

The page is an ordinary MCP client of the gateway; it never calls SEC or the model itself.
"""

import asyncio
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))  # so the demo actions import from this folder
import actions  # noqa: E402

START = (
    "uv run --env-file .env python -m multi_agent_ai_systems --http 8000 --edgar "
    "--llm-model llama3.2:3b --db demo-data.db --llm-db demo-llm.db"
)


def run(coroutine):
    try:
        return asyncio.run(coroutine)
    except Exception as exc:  # the gateway is not running or not reachable
        st.error(f"Cannot reach the gateway at {actions.URL} ({type(exc).__name__}). Start it:")
        st.code(START)
        st.stop()


def show(result: actions.ActionResult) -> None:
    st.subheader(result.title)
    if result.question:
        st.info(f"Question: {result.question}")
    st.caption(f"{result.seconds} s for the whole step")
    moved = {name: change for name, change in result.changes.items() if change}
    if moved:
        for column, (name, change) in zip(st.columns(len(moved)), 
                                          moved.items(), strict=True):
            column.metric(name, f"+{change:,}")
    else:
        st.write("No counters changed.")
    st.table(result.rows)


st.set_page_config(page_title="Shared gateway demo", layout="wide")
st.title("Shared gateway for AI applications")
st.caption("Every button sends MCP requests to the gateway, exactly as an AI application would.")

tools = run(actions.tool_names())
st.success("Gateway connected. Tools: " + ", ".join(sorted(tools)))
if "get_fact" not in tools:
    st.warning("This gateway was started without --edgar, so steps 4 and 5 will fail.")

with st.sidebar:
    st.header("Steps 1-3: LLM question")
    question = st.selectbox("Question", actions.QUESTIONS)
    st.caption("Pick a new question to show merging again; a repeated one is already cached.")

    st.header("Step 4: financial facts")
    fact_metrics = st.multiselect(
        "Metrics", list(actions.METRICS), default=["NetIncome", "Revenue"],
        format_func=actions.METRICS.get,
    )
    fact_period = st.selectbox("Period", actions.PERIODS, key="fact_period")

    st.header("Step 5: grounded answer")
    llm_metric = st.selectbox("Metric", list(actions.METRICS), format_func=actions.METRICS.get)
    llm_period = st.selectbox("Period", actions.PERIODS, key="llm_period")
    st.caption(actions.grounded_question(llm_metric, llm_period))

    st.divider()
    if st.button("Warm up the model"):
        run(actions.warm_up())
        st.toast("Model loaded")
    st.markdown(
        "**Persistence:** stop the gateway (Ctrl+C), start it again, then press step 2. "
        "The answers come back from disk (`store`) with no model call."
    )

steps = {
    "1. Three agents at once": lambda: actions.concurrent(question),
    "2. Ask again": lambda: actions.repeat(question),
    "3. Another team": lambda: actions.other_team(question),
    "4. Financial facts": lambda: actions.data_path(fact_metrics or ["NetIncome"], fact_period),
    "5. Grounded answer": lambda: actions.grounded(llm_metric, llm_period),
}
history = st.session_state.setdefault("history", [])
for column, (label, make) in zip(st.columns(len(steps)), 
                                 steps.items(), strict= True):
    if column.button(label):
        history.insert(0, run(make()))

if history:
    show(history[0])
    if len(history) > 1:
        with st.expander(f"Earlier steps ({len(history) - 1})"):
            for result in history[1:]:
                show(result)
                st.divider()

st.divider()
st.subheader("Since the gateway started")
totals = run(actions.current_totals())
names = list(totals)
for row in (names[:4], names[4:]):
    for column, name in zip(st.columns(4), 
                            row, strict= False):
        column.metric(name, f"{totals[name]:,}")