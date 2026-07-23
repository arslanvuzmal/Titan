#!/usr/bin/env python3
"""
Enterprise-Grade Seed Script for TITAN Platform Demo
Generates comprehensive synthetic data for Organizations, Users, Leads,
AI Executions, Emails, Knowledge Base Documents, Tickets, Approvals, and Metrics.
"""

import json
import random
from datetime import datetime, timedelta, timezone

def generate_demo_dataset():
    random.seed(42)
    
    # 1. Organizations & Users
    organizations = [
        {"id": "org-acme", "name": "Acme Corporation", "segment": "Enterprise - Tech", "plan": "Enterprise Scale", "users_count": 8},
        {"id": "org-global", "name": "Global Retail Inc", "segment": "Mid-Market - Retail", "plan": "Professional", "users_count": 6},
        {"id": "org-startup", "name": "StartupXYZ", "segment": "Small - SaaS", "plan": "Growth", "users_count": 4}
    ]
    
    roles = ["Admin", "Sales Ops Manager", "AI Engineer", "Support Lead", "Executive"]
    users = []
    for org in organizations:
        for i in range(1, org["users_count"] + 1):
            users.append({
                "id": f"usr-{org['id']}-{i}",
                "org_id": org["id"],
                "name": f"User {i} ({org['name'].split()[0]})",
                "email": f"user{i}@{org['name'].lower().replace(' ', '')}.com",
                "role": random.choice(roles),
                "avatar": f"https://api.dicebear.com/7.x/avataaars/svg?seed={org['id']}-{i}"
            })

    # 2. Leads & CRM Data (50+ Leads)
    industries = ["Fintech", "Healthcare", "E-Commerce", "Logistics", "Cybersecurity", "EdTech"]
    stages = ["New", "Contacted", "Qualified", "Proposal", "Negotiation", "Closed Won"]
    sources = ["Organic Search", "Paid Campaign", "Referral", "Direct Outbound", "Webinar"]
    
    leads = []
    for i in range(1, 55):
        deal_val = random.choice([5000, 12000, 25000, 48000, 85000, 150000, 320000])
        score = random.randint(35, 98)
        leads.append({
            "id": f"lead-{i:03d}",
            "company": f"Client Enterprise {i}",
            "contact_name": f"Executive {i}",
            "contact_email": f"exec{i}@client{i}.com",
            "industry": random.choice(industries),
            "score": score,
            "stage": random.choice(stages),
            "deal_value": deal_val,
            "source": random.choice(sources),
            "created_at": (datetime.now(timezone.utc) - timedelta(days=random.randint(1, 60))).isoformat()
        })

    # 3. AI Agent Task Executions (100+ Records)
    agents = ["SalesSDR", "ResearchBot", "SupportAgent", "BIEngineer", "RiskClassifier", "AuditBot"]
    tasks = []
    statuses = ["COMPLETED", "COMPLETED", "COMPLETED", "RUNNING", "PENDING_APPROVAL", "FAILED"]
    for i in range(1, 105):
        task_agent = random.choice(agents)
        status = random.choice(statuses)
        tokens = random.randint(450, 4200)
        cost = round(tokens * 0.00002, 4)
        tasks.append({
            "id": f"TSK-{1000 + i}",
            "agent": task_agent,
            "title": f"Execute {task_agent} routine #{i}",
            "status": status,
            "tokens_used": tokens,
            "cost_usd": cost,
            "duration_ms": random.randint(800, 14500),
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=random.randint(0, 168))).isoformat()
        })

    # 4. Email History (200+ Records)
    emails = []
    for i in range(1, 205):
        emails.append({
            "id": f"eml-{i:03d}",
            "to": f"contact{i}@prospect.com",
            "subject": f"Automated AI Follow-up #{i} - TITAN OS",
            "status": random.choice(["SENT", "OPENED", "CLICKED", "REPLIED"]),
            "agent_id": "SalesSDR",
            "sent_at": (datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 300))).isoformat()
        })

    # 5. Support Tickets (30+ Records)
    tickets = []
    priorities = ["Low", "Medium", "High", "Critical"]
    for i in range(1, 35):
        tickets.append({
            "id": f"TCK-{3000 + i}",
            "subject": f"System Alert / Query #{i}",
            "priority": random.choice(priorities),
            "status": random.choice(["Open", "In Progress", "Resolved"]),
            "created_at": (datetime.now(timezone.utc) - timedelta(days=random.randint(0, 15))).isoformat()
        })

    # 6. Approvals (40+ Records)
    approvals = []
    for i in range(1, 45):
        approvals.append({
            "id": f"app-{i:03d}",
            "workflow_id": f"wf-{random.randint(1000, 9999)}",
            "agent": random.choice(agents),
            "action": random.choice(["Execute Wire Transfer", "Send Bulk Campaign", "Update CRM Stage", "Delete Records"]),
            "risk_level": random.choice(["LOW", "MEDIUM", "HIGH", "CRITICAL"]),
            "status": random.choice(["PENDING", "APPROVED", "REJECTED"]),
            "timestamp": (datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 72))).isoformat()
        })

    # 7. 90-Day Business Metrics
    metrics_history = []
    base_rev = 12000
    for day in range(90, 0, -1):
        dt = (datetime.now(timezone.utc) - timedelta(days=day)).strftime("%Y-%m-%d")
        daily_rev = base_rev + random.randint(-1500, 3500) + (90 - day) * 150
        metrics_history.append({
            "date": dt,
            "revenue": daily_rev,
            "leads_converted": random.randint(5, 25),
            "tasks_executed": random.randint(120, 450),
            "csat_score": round(random.uniform(4.5, 4.95), 2)
        })

    dataset = {
        "organizations": organizations,
        "users": users,
        "leads": leads,
        "tasks": tasks,
        "emails": emails,
        "tickets": tickets,
        "approvals": approvals,
        "metrics_history": metrics_history,
        "summary": {
            "total_revenue_usd": sum(m["revenue"] for m in metrics_history),
            "total_leads": len(leads),
            "total_tasks": len(tasks),
            "seeded_at": datetime.now(timezone.utc).isoformat()
        }
    }
    
    return dataset

if __name__ == "__main__":
    data = generate_demo_dataset()
    output_path = "apps/web/src/lib/demo_seeded_data.json"
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[OK] Successfully generated enterprise demo dataset with {data['summary']['total_leads']} leads, {data['summary']['total_tasks']} task executions, and 90 days of metrics.")
    print(f"Saved to: {output_path}")
