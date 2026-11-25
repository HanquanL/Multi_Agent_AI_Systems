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
    "--llm-model llama3.2:3b --db demo-data.db --llm-db demo-llm.db "
    "--semantic semantic-team=active --semantic warm-up=shadow "
    "--semantic semantic-team=active --semantic-show-log --min-similarity 0.8"
)

def run(coroutine, failure: str = "Gateway request failed"):
    try:
        return asyncio.run(coroutine)
    except Exception as exc:
        st.error(f"{failure}: {type(exc).__name__}: {exc}")
        st.caption("If the gateway is not running, start it with:")
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
    st.caption(
        "Counter changes during this step. Requests from any other client of the gateway in the "
        "same seconds would be counted too."
    )
    if any(row.get("source") == "error" or "result" in row for row in result.rows):
        st.warning("Some requests failed; see the table below.")
    if "at the same time" in result.title and not (
        result.changes["model calls"] or result.changes["SEC requests"]
    ):
        st.warning(
            "This was already answered earlier, so there was nothing to merge. Pick a new "
            "question, or restart the gateway with the fresh-start command."
        )
    st.table(result.rows)


st.set_page_config(page_title="Shared gateway demo", layout="wide")
st.title("Shared gateway for AI applications")
st.caption("Every button sends MCP requests to the gateway, exactly as an AI application would.")

tools = run(actions.tool_names())
st.success("Gateway connected. Tools: " + ", ".join(sorted(tools)))
if "get_fact" not in tools:
    st.warning("This gateway was started without --edgar, so steps 4 - 6 will fail.")
    semantic = run(actions.semantic_modes())
    if semantic.get(actions.SEMANTIC_TEAM) != "active":
        st.warning(
        f"Semantic reuse is not active for {actions.SEMANTIC_TEAM}, so step 7 will never reuse. "
        "Start the gateway with the command below."
    )
    st.code(START)

with st.sidebar:
    st.header("Steps 1-3: LLM question")
    question = st.selectbox("Question", actions.QUESTIONS)
    st.caption("Pick a new question to show merging again; a repeated one is already cached.")
    

    st.header("Step 5: financial facts")
    fact_metrics = st.multiselect(
        "Metrics", list(actions.METRICS), default=["NetIncome", "Revenue"],
        format_func=actions.METRICS.get,
    )
    fact_period = st.selectbox("Period", actions.PERIODS, key="fact_period")

    st.header("Step 6: grounded answer")
    llm_metric = st.selectbox("Metric", list(actions.METRICS), format_func=actions.METRICS.get)
    llm_period = st.selectbox("Period", actions.PERIODS, key="llm_period")
    st.caption(actions.grounded_question(llm_metric, llm_period))

    st.header("Step 7: similar question")
    similar_case = st.selectbox("Case", list(actions.SIMILAR_CASES))
    earlier, later = actions.SIMILAR_CASES[similar_case]
    st.caption(f"First: {earlier}  \nThen: {later}")

    st.divider()
    if st.button("Warm up the model"):
        row = run(actions.warm_up(), "Warm-up failed; is Ollama running")
        st.success(f"Model ready ({row['seconds']} s)")
    if st.button("Clear page history"):
        st.session_state["history"] = []
    st.markdown(
        "**Persistence:** stop the gateway (Ctrl+C), start it again, then press step 2. "
        "The answers come back from disk (`store`) with no model call."
    )

steps = {
    "1. Three agents at once": lambda: actions.concurrent(question),
    "2. Ask again": lambda: actions.repeat(question),
    "3. Another team": lambda: actions.other_team(question),
    "4. Shared SEC record": actions.shared_record,
    "5. Financial facts": lambda: actions.data_path(fact_metrics or ["NetIncome"], fact_period),
    "6. Grounded answer": lambda: actions.grounded(llm_metric, llm_period),
    "7. Similar question": lambda: actions.similar(similar_case),
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
for row in (names[:5], names[5:]):
    for column, name in zip(st.columns(5), 
                            row, strict= False):
        column.metric(name, f"{totals[name]:,}")