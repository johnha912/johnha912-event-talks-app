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
# ruff: noqa: E402
import base64
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from google.adk.auth.credential_service.in_memory_credential_service import (
    InMemoryCredentialService,
)
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.service_factory import (
    create_artifact_service_from_options,
    create_memory_service_from_options,
    create_session_service_from_options,
)
from google.adk.runners import Runner
from google.genai import types
from pydantic import BaseModel

from expense_agent.agent import app as agent_app
from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback

# Configure standard Python logging for console logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fast_api_app")

setup_telemetry()

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=False,  # Set otel_to_cloud=False per checklist
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"

# Instantiate Runner with local storage services (so it shares session DB with the FastAPI app)
session_service = create_session_service_from_options(
    base_dir=AGENT_DIR,
    session_service_uri=session_service_uri,
)
artifact_service = create_artifact_service_from_options(
    base_dir=AGENT_DIR,
    artifact_service_uri=artifact_service_uri,
)
memory_service = create_memory_service_from_options(
    base_dir=AGENT_DIR,
    memory_service_uri=None,
)
credential_service = InMemoryCredentialService()

runner = Runner(
    app=agent_app,
    session_service=session_service,
    artifact_service=artifact_service,
    memory_service=memory_service,
    credential_service=credential_service,
)


class PubSubMessage(BaseModel):
    data: str
    messageId: str | None = None
    publishTime: str | None = None


class PubSubEnvelope(BaseModel):
    message: PubSubMessage
    subscription: str


import uuid


@app.post("/")
@app.post("/apps/{app_name}/trigger/pubsub")
async def handle_pubsub(envelope: PubSubEnvelope, app_name: str | None = None):
    """Event-driven endpoint for Pub/Sub push notifications."""
    # 1. Normalize the fully-qualified subscription path down to a short name
    # e.g., "projects/my-project/subscriptions/expense-sub" -> "expense-sub"
    sub_short_name = envelope.subscription.split("/")[-1]

    # 2. Decode incoming base64 Pub/Sub payload
    try:
        decoded_bytes = base64.b64decode(envelope.message.data)
        decoded_str = decoded_bytes.decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to decode base64 data: {e}")
        return {"status": "error", "message": "Invalid base64 encoding"}

    # 3. Create or load the session, using messageId as session_id to keep it unique
    session_id = envelope.message.messageId or f"session-{uuid.uuid4().hex[:8]}"
    user_id = sub_short_name

    logger.info(f"Instantiating ADK session '{session_id}' for user '{user_id}'")

    session = await runner.session_service.create_session(
        app_name="expense_agent",
        user_id=user_id,
        session_id=session_id,
    )

    msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text=decoded_str)],
    )

    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=msg,
    ):
        events.append(event)
        logger.info(
            f"Event yielded: author={event.author}, output={event.output}, interrupted={event.interrupted}"
        )

    return {
        "status": "success",
        "session_id": session_id,
        "user_id": user_id,
        "events_count": len(events),
    }


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback."""
    logger.info(f"Feedback received: {feedback.model_dump()}")
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
