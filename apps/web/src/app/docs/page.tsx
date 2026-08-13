"use client";

import React from 'react';
import { BookOpen, Code, ShieldCheck, Zap, HelpCircle, Terminal } from 'lucide-react';

export default function DocumentationPage() {
  return (
    <>
      <div className="max-w-5xl mx-auto py-6 space-y-8 text-gray-800">
        {/* Header */}
        <div className="border-b pb-4">
          <h1 className="text-3xl font-bold text-gray-900 flex items-center">
            <BookOpen className="w-8 h-8 mr-3 text-[#3c8dbc]" />
            TITAN Platform Documentation & Architecture
          </h1>
          <p className="text-gray-500 mt-1">
            Complete technical guide, API references, and architectural patterns powering TITAN OS.
          </p>
        </div>

        {/* Quick Navigation Cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm hover:border-[#3c8dbc] transition-colors">
            <Zap className="w-6 h-6 text-amber-500 mb-2" />
            <h3 className="font-semibold text-gray-900">Getting Started</h3>
            <p className="text-xs text-gray-500 mt-1">First-time environment setup, virtualenvs, and monorepo commands.</p>
          </div>
          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm hover:border-[#3c8dbc] transition-colors">
            <ShieldCheck className="w-6 h-6 text-green-500 mb-2" />
            <h3 className="font-semibold text-gray-900">Human-in-the-Loop</h3>
            <p className="text-xs text-gray-500 mt-1">Configuring RiskClassifier policies, approval queues, and temporal signals.</p>
          </div>
          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm hover:border-[#3c8dbc] transition-colors">
            <Code className="w-6 h-6 text-purple-500 mb-2" />
            <h3 className="font-semibold text-gray-900">API Reference</h3>
            <p className="text-xs text-gray-500 mt-1">FastAPI REST endpoints, OpenAPI schemas, and WebSocket event payloads.</p>
          </div>
        </div>

        {/* Section 1: Monorepo Architecture */}
        <div className="bg-white rounded-lg border border-gray-200 p-6 space-y-4">
          <h2 className="text-xl font-bold text-gray-900 flex items-center">
            <Terminal className="w-5 h-5 mr-2 text-[#3c8dbc]" />
            1. Monorepo Structure
          </h2>
          <p className="text-sm text-gray-600 leading-relaxed">
            TITAN is structured as a production pnpm monorepo consisting of two primary application components:
          </p>
          <pre className="bg-gray-900 text-green-400 p-4 rounded text-xs font-mono overflow-x-auto">
{`titan/
├── apps/
│   ├── api/             # FastAPI + Temporal Workflows + LangGraph Agents
│   │   ├── app/
│   │   │   ├── agents/  # SDR, Research, RiskClassifier agents
│   │   │   ├── core/    # Config, Security, SSRF proxy
│   │   │   └── main.py  # FastAPI app entry point
│   │   └── tests/       # Pytest unit & chaos tests
│   └── web/             # Next.js 16 + TailwindCSS Dashboard
│       └── src/
│           ├── app/     # App Router pages
│           └── components/ # UI Widgets & Execution Traces
└── scripts/             # Seed scripts & CI tools`}
          </pre>
        </div>

        {/* Section 2: Golden Path Execution */}
        <div className="bg-white rounded-lg border border-gray-200 p-6 space-y-4">
          <h2 className="text-xl font-bold text-gray-900 flex items-center">
            <HelpCircle className="w-5 h-5 mr-2 text-[#3c8dbc]" />
            2. The 16-Step Golden Path Workflow
          </h2>
          <div className="space-y-2 text-xs text-gray-600">
            <div className="p-2 bg-gray-50 border rounded font-mono">Step 01: Inbound Webhook receives lead payload</div>
            <div className="p-2 bg-gray-50 border rounded font-mono">Step 02: Pydantic v2 validates domain & contract</div>
            <div className="p-2 bg-gray-50 border rounded font-mono">Step 03: Temporal Workflow initialized with durable execution state</div>
            <div className="p-2 bg-gray-50 border rounded font-mono">Step 04: ResearchBot agent performs SSRF-safe web scraping</div>
            <div className="p-2 bg-gray-50 border rounded font-mono">Step 05: LangGraph scores lead fit (0-100) using GPT-4o</div>
            <div className="p-2 bg-gray-50 border rounded font-mono">Step 06: RiskClassifier evaluates proposed outreach action</div>
            <div className="p-2 bg-gray-50 border rounded font-mono">Step 07: High-risk action intercepted & paused for HITL approval</div>
            <div className="p-2 bg-gray-50 border rounded font-mono">Step 08: Admin approves action via Command Center UI</div>
          </div>
        </div>

        {/* Section 3: FAQ */}
        <div className="bg-white rounded-lg border border-gray-200 p-6 space-y-4">
          <h2 className="text-xl font-bold text-gray-900">3. Frequently Asked Questions (FAQ)</h2>
          <div className="space-y-3 text-sm">
            <div>
              <h4 className="font-semibold text-gray-900">How does TITAN prevent SSRF attacks?</h4>
              <p className="text-gray-600 text-xs mt-0.5">
                All outbound HTTP requests from agents pass through an internal security proxy that resolves DNS and validates IP ranges against private/internal blocks (10.0.0.0/8, 127.0.0.1, 169.254.169.254).
              </p>
            </div>
            <div>
              <h4 className="font-semibold text-gray-900">What happens if an external LLM API fails mid-workflow?</h4>
              <p className="text-gray-600 text-xs mt-0.5">
                Temporal durable execution automatically retries the failed activity with exponential backoff without losing any workflow state.
              </p>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
