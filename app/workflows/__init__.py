"""Workflow routing layer.

The first implementation keeps workflows as lightweight route definitions that
reuse the existing agents. Individual files can later be replaced with
LangGraph StateGraph implementations without changing IntentAgent output.
"""
