#!/usr/bin/env python3
import json
import urllib.request
import datetime
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/report_daily_work/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('report_daily_work')
    except Exception:
        pass


TOKEN = "stk_gqxOVX6yE1_iePmNsI6qWdHblpjVKSzuDoFs4VgrNHA"
URL = "https://stackyy.vercel.app/api/mcp"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

def call_mcp_tool(tool_name, arguments=None):
    if arguments is None:
        arguments = {}
    req_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments
        }
    }
    try:
        req = urllib.request.Request(URL, data=json.dumps(req_body).encode('utf-8'), headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_resp = resp.read().decode('utf-8')
            for line in raw_resp.splitlines():
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    result = data.get("result", {})
                    content = result.get("content", [])
                    for item in content:
                        if item.get("type") == "text":
                            return json.loads(item.get("text", "[]"))
                except Exception:
                    pass
    except Exception as e:
        print(f"Error calling {tool_name}: {e}")
    return []

def main():
    date_str = datetime.datetime.now().strftime('%A, %B %d, %Y')
    print(f"📋 *Stacky Daily Project & Task Updates*\n_{date_str}_\n")

    workspaces = call_mcp_tool("list_workspaces")
    if not workspaces:
        print("No workspaces found.")
        return

    # Fetch all tasks across workspaces
    all_tasks = call_mcp_tool("list_tasks")
    
    # Map project names
    project_map = {}
    workspace_map = {ws.get("id"): ws.get("name") for ws in workspaces}

    for ws in workspaces:
        ws_id = ws.get("id")
        projects = call_mcp_tool("list_projects", {"workspaceId": ws_id})
        for proj in projects:
            project_map[proj.get("id")] = {
                "name": proj.get("name"),
                "status": proj.get("status", "active"),
                "workspace": ws.get("name")
            }

    # Group tasks by Workspace -> Project
    # Structure: { ws_name: { proj_name: [tasks] } }
    grouped = {}
    for task in all_tasks:
        ws_id = task.get("workspaceId")
        ws_name = workspace_map.get(ws_id, "Other")
        
        proj_id = task.get("projectId")
        proj_info = project_map.get(proj_id, {})
        proj_name = proj_info.get("name", "General / No Project")
        
        if ws_name not in grouped:
            grouped[ws_name] = {}
        if proj_name not in grouped[ws_name]:
            grouped[ws_name][proj_name] = []
        
        grouped[ws_name][proj_name].append(task)

    if not grouped:
        print("No tasks found across projects.")
        return

    for ws_name, projects in grouped.items():
        print(f"🏢 *Workspace: {ws_name}*")
        
        for proj_name, tasks in projects.items():
            print(f"\n📂 *Project: {proj_name}*")
            
            in_progress = [t for t in tasks if t.get("status") in ["in_progress", "planned"]]
            backlog = [t for t in tasks if t.get("status") == "backlog"]
            done = [t for t in tasks if t.get("status") == "done"]
            
            if in_progress:
                print("  *Active / Planned Tasks:*")
                for t in in_progress:
                    priority = t.get("priority", "P2")
                    tags = f" `[{', '.join(t.get('tags', []))}]`" if t.get("tags") else ""
                    print(f"  • [{priority}] {t.get('title')}{tags}")
            
            if backlog:
                print(f"  *Backlog:* {len(backlog)} pending task(s)")
                # Show top 3 backlog items
                for t in backlog[:3]:
                    print(f"    - {t.get('title')}")
                if len(backlog) > 3:
                    print(f"    - ... and {len(backlog) - 3} more")
            
            if done:
                print(f"  *Completed:* {len(done)} task(s) closed ✅")
        print("\n" + "─" * 28 + "\n")

if __name__ == "__main__":
    main()
