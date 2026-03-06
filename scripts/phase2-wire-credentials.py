#!/usr/bin/env python3
"""
Run AFTER manually creating credentials in n8n UI.
Discovers credential IDs and wires them into all workflow nodes.
Also fixes workflow_history (the Two-Table Rule).

Usage: python3 phase2-wire-credentials.py --env-file ../.env [--n8n-folder /.n8n]
"""
import argparse, json, sqlite3

parser = argparse.ArgumentParser()
parser.add_argument('--env-file', default='.env')
parser.add_argument('--n8n-folder', default='/.n8n')
args = parser.parse_args()

db_path = f"{args.n8n_folder}/database.sqlite"
conn = sqlite3.connect(db_path)

print("=== Discovered credentials ===")
cred_map = {}
for row in conn.execute("SELECT id, name, type FROM credentials_entity ORDER BY type"):
    print(f"  [{row[2]}] {row[1]} -> {row[0]}")
    cred_map[row[2]] = row[0]

print("\n=== Fixing credential IDs in all workflows ===")
for wf_id, wf_name in conn.execute("SELECT id, name FROM workflow_entity").fetchall():
    row = conn.execute("SELECT nodes FROM workflow_entity WHERE id=?", [wf_id]).fetchone()
    if not row or not row[0]:
        continue
    nodes = json.loads(row[0])
    changed = False
    for node in nodes:
        for ctype, cval in node.get('credentials', {}).items():
            if isinstance(cval, dict) and cval.get('id') == 'REPLACE_WITH_YOUR_CREDENTIAL_ID':
                real_id = cred_map.get(ctype)
                if real_id:
                    cval['id'] = real_id
                    changed = True
                    print(f"  [{wf_name}] {node['name']}: {ctype} -> {real_id}")
    if changed:
        fixed = json.dumps(nodes)
        conn.execute("UPDATE workflow_entity SET nodes=? WHERE id=?", [fixed, wf_id])
        # Two-Table Rule: also fix workflow_history
        hist = conn.execute(
            "SELECT nodes FROM workflow_history WHERE workflowId=?", [wf_id]
        ).fetchone()
        if hist and hist[0]:
            hist_nodes = json.loads(hist[0])
            for node in hist_nodes:
                for ctype, cval in node.get('credentials', {}).items():
                    if isinstance(cval, dict) and cval.get('id') == 'REPLACE_WITH_YOUR_CREDENTIAL_ID':
                        real_id = cred_map.get(ctype)
                        if real_id:
                            cval['id'] = real_id
            conn.execute(
                "UPDATE workflow_history SET nodes=? WHERE workflowId=?",
                [json.dumps(hist_nodes), wf_id]
            )

conn.commit()
conn.close()
print("\nCredentials wired into all workflows (both tables).")
print("\nFINAL STEP: In n8n UI, toggle 'n8n-claw Agent' OFF then ON to sync Telegram webhook.")
