import asyncio
import json
import os
import glob
import subprocess
import sys

# Import trace generator
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import tests.eval.generate_traces as gt

async def run_all():
    # 1. Run trace generator
    print("Step 1: Generating traces...")
    await gt.main()
    
    # 2. Run agents-cli eval grade
    print("Step 2: Grading traces...")
    cmd = [
        "uv", "run", "agents-cli", "eval", "grade",
        "--traces", "artifacts/traces/generated_traces.json",
        "--config", "tests/eval/eval_config.yaml"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    print("Grading Output:\n", result.stdout)
    if result.stderr:
        print("Grading Error:\n", result.stderr)
        
    # 3. Locate the results file
    results_files = glob.glob("artifacts/grade_results/results_*.json")
    if not results_files:
        print("Error: No results file found under artifacts/grade_results/")
        return
        
    latest_file = max(results_files, key=os.path.getmtime)
    print(f"Reading results from: {latest_file}")
    
    with open(latest_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    # 4. Parse case results and print scorecard
    print("\n" + "="*80)
    print(" E VAL U A T I O N   S C O R E C A R D")
    print("="*80)
    
    cases = data.get("eval_case_results", [])
    for c in cases:
        case_id = c.get("eval_case_id") or c.get("eval_id")
        print(f"\nCase: {case_id}")
        print("-" * len(f"Case: {case_id}"))
        
        # Look for the prompt description
        prompt_content = c.get("prompt", {}).get("parts", [{}])[0].get("text", "")
        try:
            payload = json.loads(prompt_content)
            desc = payload.get("description", prompt_content)
            amount = payload.get("amount", "unknown")
            print(f"  Expense Description: '{desc}' (Amount: ${amount})")
        except Exception:
            print(f"  Prompt: {prompt_content}")
            
        metrics = c.get("eval_metric_results", [])
        for m in metrics:
            metric_name = m.get("metric_name")
            score = m.get("score")
            # In LLMMetric, explanation might be inside details or logic
            explanation = m.get("explanation") or ""
            # Let's check if explanation is in rubric_scores or details
            if not explanation and "details" in m:
                details = m["details"]
                if "rubric_scores" in details and details["rubric_scores"]:
                    explanation = details["rubric_scores"][0].get("rationale")
            print(f"  - Metric: {metric_name} | Score: {score}/5")
            if explanation:
                print(f"    Reasoning: {explanation}")
                
    print("\n" + "="*80)
    
if __name__ == "__main__":
    asyncio.run(run_all())
