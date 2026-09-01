import json
import os
import time


node_id = os.environ.get("VIBE_NODE_ID", "")
role = os.environ.get("VIBE_TASK_ROLE", "")

if node_id == "slow":
    time.sleep(30)
elif node_id.startswith("unknown"):
    print(json.dumps({"event": "unknown", "data": {"reason": "simulated provider timeout"}}))
elif role == "reviewer":
    print(json.dumps({"event": "accepted", "data": {"evidence": "fixture review accepted"}}))
else:
    print(json.dumps({"event": "complete", "data": {"evidence": "fixture developer delivered"}}))
