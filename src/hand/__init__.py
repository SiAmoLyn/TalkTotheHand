"""HAND-0: offline by default; tracing and analytics forcibly disabled."""

import os

# Set before any LangGraph imports; inherited tracing settings cannot override this.
os.environ["LANGGRAPH_CLI_NO_ANALYTICS"] = "1"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
