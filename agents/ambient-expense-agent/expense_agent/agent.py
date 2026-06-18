# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
import os
import re

import google.auth
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import EventActions
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.workflow import FunctionNode, Workflow
from google.auth.exceptions import DefaultCredentialsError
from google.genai import types
from pydantic import BaseModel, Field

from expense_agent.config import MODEL_NAME, THRESHOLD

# Setup environment fallback for authentication
use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() in (
    "true",
    "1",
)

if use_vertex:
    try:
        _, project_id = google.auth.default()
        os.environ["GOOGLE_CLOUD_PROJECT"] = os.environ.get(
            "GOOGLE_CLOUD_PROJECT", project_id
        )
        os.environ["GOOGLE_CLOUD_LOCATION"] = os.environ.get(
            "GOOGLE_CLOUD_LOCATION", "global"
        )
    except DefaultCredentialsError:
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"


# --- Schemas ---


class RiskAssessment(BaseModel):
    risk_score: int = Field(description="Risk score from 1 (low) to 10 (high)")
    risk_factors: list[str] = Field(description="List of risk factors identified")
    summary: str = Field(description="Summary of the risk assessment")


# --- Nodes ---


def parse_input(ctx: Context, node_input: types.Content) -> Event:
    """Parses incoming Pub/Sub base64 or plain JSON expense payload and routes it."""
    text_content = ""
    if node_input and node_input.parts:
        text_content = node_input.parts[0].text or ""

    # Load raw payload
    try:
        payload = json.loads(text_content)
    except Exception:
        # Fallback if text is not valid JSON
        payload = {"description": text_content}

    # Handle Pub/Sub base64 data envelope
    data = payload.get("data")
    if isinstance(data, str):
        try:
            decoded_bytes = base64.b64decode(data)
            data = json.loads(decoded_bytes.decode("utf-8"))
        except Exception:
            pass

    if not isinstance(data, dict):
        data = payload

    # Extract expense details
    amount = float(data.get("amount", 0.0))
    submitter = str(data.get("submitter", "Unknown"))
    category = str(data.get("category", "General"))
    description = str(data.get("description", "No description"))
    date = str(data.get("date", "Unknown"))

    expense = {
        "amount": amount,
        "submitter": submitter,
        "category": category,
        "description": description,
        "date": date,
    }

    # Route based on USD threshold
    route = "auto_approve" if amount < THRESHOLD else "llm_review"

    return Event(
        output=expense,
        actions=EventActions(route=route, state_delta={"expense": expense}),
    )


def auto_approve(node_input: dict):
    """Auto-approves expenses under the threshold."""
    result = {
        "status": "APPROVED",
        "reason": f"Auto-approved: amount is under the ${THRESHOLD:.2f} threshold.",
        "expense": node_input,
    }

    yield Event(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f"Expense auto-approved. Details:\n{json.dumps(result, indent=2)}"
                )
            ],
        )
    )
    yield Event(output=result)


def security_screen(ctx: Context, node_input: dict) -> Event:
    """Scrubs personal data (PIII) and checks for prompt injection in description."""
    # Obtain the current expense details
    expense = dict(node_input or ctx.state.get("expense", {}))
    description = expense.get("description", "")

    # 1. PII Scrubbing (SSNs & Credit Card Numbers)
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b"
    cc_pattern = r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b|\b\d{4}[- ]?\d{6}[- ]?\d{5}\b|\b\d{13,16}\b"

    redacted = []
    cleaned_desc = description

    if re.search(ssn_pattern, cleaned_desc):
        cleaned_desc = re.sub(ssn_pattern, "[REDACTED SSN]", cleaned_desc)
        redacted.append("SSN")

    if re.search(cc_pattern, cleaned_desc):
        cleaned_desc = re.sub(cc_pattern, "[REDACTED CREDIT CARD]", cleaned_desc)
        redacted.append("Credit Card")

    # Update description in the expense object
    expense["description"] = cleaned_desc

    # 2. Prompt Injection Checking
    injection_phrases = [
        "ignore previous instructions",
        "ignore all rules",
        "system override",
        "override approval",
        "bypass audit",
        "bypass llm",
        "auto-approve this",
        "auto-approve expense",
    ]
    desc_lower = cleaned_desc.lower()
    is_injection = any(phrase in desc_lower for phrase in injection_phrases)

    state_delta = {
        "expense": expense,
        "redacted_categories": redacted,
    }

    if is_injection:
        state_delta["security_event"] = True
        # Bypass LLM and route straight to human approval
        # We output a mock RiskAssessment dictionary to alert the human auditor
        warning_assessment = {
            "risk_score": 10,
            "risk_factors": ["PROMPT INJECTION DETECTED - SECURITY EVENT"],
            "summary": (
                "The expense description contained potential prompt injection attempts to bypass auditing rules. "
                "The Gemini LLM review was bypassed for safety, and this event has been flagged for manual security review."
            ),
        }
        return Event(
            output=warning_assessment,
            actions=EventActions(route="bypass_to_human", state_delta=state_delta),
        )

    # Clean expense, route to LLM review
    return Event(
        output=expense,
        actions=EventActions(route="llm_review", state_delta=state_delta),
    )


# LLM Node to perform Risk Assessment
risk_reviewer = LlmAgent(
    name="risk_reviewer",
    model=Gemini(model=MODEL_NAME),
    instruction=(
        "You are an expert corporate expense auditor. Analyze the "
        "provided expense details and evaluate the risk. Look for mismatching categories, "
        "suspicious descriptions, or potentially non-compliant spending. "
        "Provide a risk score from 1 to 10, list any specific risk factors, "
        "and provide a short summary of your assessment."
    ),
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


async def human_approval(ctx: Context, node_input: dict):
    """Pauses workflow for human review, and processes decision once resumed."""
    expense = ctx.state.get("expense")

    # Pause if decision is not in resume inputs
    if not ctx.resume_inputs or "decision" not in ctx.resume_inputs:
        redacted = ctx.state.get("redacted_categories", [])
        redacted_str = f"- Redacted Info: {', '.join(redacted)}\n" if redacted else ""

        message_text = (
            f"🚨 ALERT: High-value expense requires manual approval!\n\n"
            f"Expense Details:\n"
            f"- Submitter: {expense.get('submitter')}\n"
            f"- Amount: ${expense.get('amount'):.2f}\n"
            f"- Category: {expense.get('category')}\n"
            f"- Description: {expense.get('description')}\n"
            f"- Date: {expense.get('date')}\n"
            f"{redacted_str}\n"
            f"LLM Risk Assessment:\n"
            f"- Risk Score: {node_input.get('risk_score')}/10\n"
            f"- Risk Factors: {', '.join(node_input.get('risk_factors', []))}\n"
            f"- Summary: {node_input.get('summary')}\n\n"
            f"Please review and reply with 'approve' or 'reject' to complete the workflow."
        )
        yield RequestInput(interrupt_id="decision", message=message_text)
        return

    # Process decision
    decision_val = ctx.resume_inputs["decision"]
    if isinstance(decision_val, dict):
        decision_text = str(
            decision_val.get(
                "decision",
                decision_val.get("output", next(iter(decision_val.values()))),
            )
        )
    else:
        decision_text = str(decision_val)
    decision_text = decision_text.strip().lower()
    if "approve" in decision_text:
        status = "APPROVED"
    elif "reject" in decision_text:
        status = "REJECTED"
    else:
        status = "REJECTED"
        decision_text = f"Rejected due to invalid/ambiguous input: '{decision_text}'"

    result = {
        "status": status,
        "reason": f"Manual decision: {decision_text}",
        "expense": expense,
        "risk_assessment": node_input,
    }

    yield Event(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f"Expense {status}. Decision recorded: {decision_text}"
                )
            ],
        )
    )
    yield Event(output=result)


# --- Workflow Graph Definition ---

human_approval_node = FunctionNode(
    func=human_approval,
    name="human_approval",
    rerun_on_resume=True,
)

root_agent = Workflow(
    name="expense_approval_workflow",
    edges=[
        ("START", parse_input),
        (
            parse_input,
            {"auto_approve": auto_approve, "llm_review": security_screen},
        ),
        (
            security_screen,
            {"llm_review": risk_reviewer, "bypass_to_human": human_approval_node},
        ),
        (risk_reviewer, human_approval_node),
    ],
)

# App instance
app = App(
    root_agent=root_agent,
    name="expense_agent",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
