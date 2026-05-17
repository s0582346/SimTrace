"""Supervisor Agent - routes work to specialist agents and talks to the user.

Per the architecture diagram, the Supervisor is the single entry point for the
user. It clarifies ambiguous problem descriptions and delegates to the
Modeler / Verifier / Simulator / Analyst specialists. This module currently
implements only the user-facing chat surface for smoke-testing the Groq API.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq

SYSTEM_PROMPT = """You are the Supervisor of a multi-agent system that designs,
verifies, runs, and analyzes Discrete Event Simulation (DES) models.

You coordinate four specialist agents:
- Modeler: turns requirements into a conceptual DES model and SimPy code.
- Verifier: validates schema, syntax, and executability.
- Simulator: runs the simulation with replications.
- Analyst: interprets results against expected KPIs.

For now you are in chat-only mode for testing. Respond conversationally, and if
the user describes a simulation problem, briefly state which specialist you
would invoke first and why."""


class SupervisorAgent:
    """Supervisor agent backed by a Groq-hosted chat model."""

    def __init__(
        self,
        model: str = "llama-3.1-8b-instant",
        temperature: float = 0.3,
        api_key: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Add it to your .env file."
            )
        self.llm = ChatGroq(model=model, temperature=temperature, api_key=key)
        self.history: list = [SystemMessage(content=SYSTEM_PROMPT)]

    def chat(self, user_message: str) -> str:
        self.history.append(HumanMessage(content=user_message))
        response = self.llm.invoke(self.history)
        self.history.append(AIMessage(content=response.content))
        return response.content


def _repl() -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    try:
        agent = SupervisorAgent()
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)

    print("Supervisor ready (Groq). Type 'exit' or Ctrl+C to quit.\n")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in {"exit", "quit"}:
            break
        try:
            reply = agent.chat(user)
        except Exception as exc:
            print(f"[error] {exc}", file=sys.stderr)
            continue
        print(f"sup> {reply}\n")


if __name__ == "__main__":
    _repl()
