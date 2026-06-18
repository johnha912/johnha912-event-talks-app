import asyncio
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from google.adk.runners import InMemoryRunner
from google.genai import types
from expense_agent.agent import app

def map_event_to_sdk_event(e):
    sdk_event = {"author": e.author or "unknown"}
    if e.content:
        parts = []
        for p in getattr(e.content, 'parts', []) or []:
            part_dict = {}
            if getattr(p, 'text', None):
                part_dict['text'] = p.text
            if getattr(p, 'function_call', None):
                fc = p.function_call
                part_dict['function_call'] = {
                    'name': fc.name,
                    'args': fc.args,
                    'id': fc.id
                }
            if getattr(p, 'function_response', None):
                fr = p.function_response
                part_dict['function_response'] = {
                    'name': fr.name,
                    'response': fr.response,
                    'id': fr.id
                }
            if part_dict:
                parts.append(part_dict)
        sdk_event["content"] = {
            "role": getattr(e.content, 'role', 'model') or 'model',
            "parts": parts
        }
    elif e.output is not None:
        sdk_event["content"] = {
            "role": "model",
            "parts": [{"text": json.dumps(e.output)}]
        }
    return sdk_event

def get_final_response(session_events):
    for e in reversed(session_events):
        if e.content and e.content.parts:
            text = "".join(p.text for p in e.content.parts if p.text)
            if text.strip():
                return text
    for e in reversed(session_events):
        if e.output is not None:
            return json.dumps(e.output)
    return "No response"

async def run_eval_case(runner, case):
    prompt_text = case["prompt"]["parts"][0]["text"]
    
    # Check if there is prompt injection in description
    try:
        payload = json.loads(prompt_text)
        description = payload.get("description", "").lower()
    except Exception:
        description = prompt_text.lower()
        
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
    is_injection = any(phrase in description for phrase in injection_phrases)
    decision = "reject" if is_injection else "approve"
    
    session = await runner.session_service.create_session(
        app_name="expense_agent", user_id="eval_user"
    )
    
    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=prompt_text)]
    )
    
    # Run the initial steps
    async for event in runner.run_async(
        user_id="eval_user", session_id=session.id, new_message=new_message
    ):
        pass
        
    # Check if we need to resume (manual approval node)
    while True:
        session = await runner.session_service.get_session(
            app_name="expense_agent", user_id="eval_user", session_id=session.id
        )
        interrupt_event = None
        for e in reversed(session.events):
            if e.long_running_tool_ids:
                interrupt_event = e
                break
        
        if not interrupt_event:
            break
            
        interrupt_id = list(interrupt_event.long_running_tool_ids)[0]
        msg_resume = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="decision", id=interrupt_id, response={"decision": decision}
                    )
                )
            ],
        )
        
        async for event in runner.run_async(
            user_id="eval_user", session_id=session.id, new_message=msg_resume
        ):
            pass
            
    # Session is now finished. Reload session to get all events.
    session = await runner.session_service.get_session(
        app_name="expense_agent", user_id="eval_user", session_id=session.id
    )
    
    sdk_events = [map_event_to_sdk_event(e) for e in session.events]
    final_response_text = get_final_response(session.events)
    
    eval_case = {
        "eval_id": case["eval_case_id"],
        "prompt": case["prompt"],
        "responses": [
            {
                "response": {
                    "role": "model",
                    "parts": [{"text": final_response_text}]
                }
            }
        ],
        "agent_data": {
            "turns": [
                {
                    "turn_index": 0,
                    "events": sdk_events
                }
            ]
        }
    }
    return eval_case

async def main():
    dataset_path = Path("tests/eval/datasets/basic-dataset.json")
    output_path = Path("artifacts/traces/generated_traces.json")
    
    with open(dataset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    runner = InMemoryRunner(app=app)
    eval_cases = []
    
    for case in data["eval_cases"]:
        print(f"Running evaluation case: {case['eval_case_id']}...")
        eval_case = await run_eval_case(runner, case)
        eval_cases.append(eval_case)
        print(f"Case {case['eval_case_id']} completed.")
        
    result = {
        "eval_cases": eval_cases
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        
    print(f"Traces successfully saved to {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
