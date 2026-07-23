"use client";

import seededData from './demo_seeded_data.json';

export interface LiveActivityItem {
  id: string;
  type: 'LEAD' | 'TASK' | 'APPROVAL' | 'EMAIL' | 'TICKET';
  title: string;
  subtitle: string;
  status: string;
  timestamp: string;
  badgeColor: string;
}

export const getSeededData = () => {
  return seededData;
};

export const INITIAL_REALTIME_ACTIVITIES: LiveActivityItem[] = [
  {
    id: "act-101",
    type: "LEAD",
    title: "High-Value Lead Scored (94/100)",
    subtitle: "Acme Corp ($150k ARR Potential)",
    status: "QUALIFIED",
    timestamp: "Just now",
    badgeColor: "bg-green-100 text-green-700 border-green-200"
  },
  {
    id: "act-102",
    type: "APPROVAL",
    title: "HITL Risk Assessment Triggered",
    subtitle: "FinanceBot requested wire transfer $45,000",
    status: "PENDING",
    timestamp: "2 mins ago",
    badgeColor: "bg-amber-100 text-amber-700 border-amber-200"
  },
  {
    id: "act-103",
    type: "TASK",
    title: "LangGraph Multi-Agent Cycle Completed",
    subtitle: "ResearchBot -> SDR -> Risk Classifier (1,240 tokens)",
    status: "SUCCESS",
    timestamp: "5 mins ago",
    badgeColor: "bg-blue-100 text-blue-700 border-blue-200"
  },
  {
    id: "act-104",
    type: "EMAIL",
    title: "Outreach Sequence Dispatched",
    subtitle: "Sent 45 personalized emails via SalesSDR",
    status: "SENT",
    timestamp: "8 mins ago",
    badgeColor: "bg-purple-100 text-purple-700 border-purple-200"
  },
  {
    id: "act-105",
    type: "TICKET",
    title: "Support Escalation Resolved",
    subtitle: "Ticket #3014 resolved automatically via RAG Knowledge Base",
    status: "RESOLVED",
    timestamp: "12 mins ago",
    badgeColor: "bg-emerald-100 text-emerald-700 border-emerald-200"
  }
];

export const DEMO_SCENARIOS = [
  {
    id: "scenario-golden-path",
    title: "Golden Path: Inbound Lead to Closed Deal",
    description: "Watch TITAN SDR evaluate, score, gather research, route through Human-in-the-Loop, and trigger CRM updates.",
    steps: 16,
    badge: "16 Steps • Sales Automation"
  },
  {
    id: "scenario-crisis",
    title: "Crisis Management & Support Escalation",
    description: "Simulates an enterprise outage ticket automatically invoking the Risk Classifier and escalating to human approval.",
    steps: 8,
    badge: "8 Steps • Risk Mitigation"
  },
  {
    id: "scenario-bi-report",
    title: "Automated Executive Intelligence Report",
    description: "Demonstrates BI Agent aggregating 90 days of revenue data and posting executive summary alerts.",
    steps: 10,
    badge: "10 Steps • Analytics"
  }
];
