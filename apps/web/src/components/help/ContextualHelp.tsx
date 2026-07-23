"use client";

import React from 'react';
import { HelpCircle, Info, ShieldCheck, Workflow, Database, X } from 'lucide-react';

export function InfoTooltip({ text }: { text: string }) {
  return (
    <div className="relative group inline-block ml-1.5 align-middle">
      <HelpCircle className="w-3.5 h-3.5 text-gray-400 hover:text-[#3c8dbc] cursor-pointer transition-colors" />
      <div className="absolute left-1/2 -translate-x-1/2 bottom-full mb-2 hidden group-hover:block w-48 p-2 bg-gray-900 text-white text-[11px] rounded shadow-lg z-50 pointer-events-none">
        {text}
        <div className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
      </div>
    </div>
  );
}

export function FeatureShowcaseModal({ 
  topic, 
  isOpen, 
  onClose 
}: { 
  topic: 'SCORING' | 'HITL' | 'RAG' | 'TEMPORAL'; 
  isOpen: boolean; 
  onClose: () => void; 
}) {
  if (!isOpen) return null;

  const content = {
    SCORING: {
      title: "How AI Lead Scoring Works",
      subtitle: "Multi-Criteria Evaluation Engine",
      icon: <Info className="w-6 h-6 text-blue-400" />,
      steps: [
        "1. Ingestion: Webhook captures raw lead domain, company headcount, & intent signal.",
        "2. Enrichment: ResearchBot fetches Clearbit/LinkedIn data & tech stack.",
        "3. Scoring Model: GPT-4o evaluates ICP fit, budget authority, & urgent pain points (0-100).",
        "4. Action Dispatch: Scores ≥ 80 automatically spawn personalized outreach sequences."
      ]
    },
    HITL: {
      title: "Human-in-the-Loop (HITL) Workflow",
      subtitle: "Risk-Gated Governance Architecture",
      icon: <ShieldCheck className="w-6 h-6 text-amber-400" />,
      steps: [
        "1. Risk Assessment: RiskClassifier evaluates tool invocation parameters.",
        "2. Interception: Actions with risk > Medium pause execution in Temporal.",
        "3. Admin Notification: Approval card delivered to Command Center with full context.",
        "4. Resume/Cancel: Human 1-click decision safely resumes or cancels execution."
      ]
    },
    RAG: {
      title: "Retrieval-Augmented Generation (RAG)",
      subtitle: "Grounding Agent Responses in Enterprise Data",
      icon: <Database className="w-6 h-6 text-purple-400" />,
      steps: [
        "1. Ingestion & Embedding: SOPs & Knowledge Docs embedded into Qdrant Vector DB.",
        "2. Semantic Query: User ticket or query generates 1536-dim embedding vector.",
        "3. Top-K Match: Cosine similarity retrieves relevant document chunks.",
        "4. Grounded Generation: LLM crafts hallucination-free response using retrieved context."
      ]
    },
    TEMPORAL: {
      title: "Temporal Workflow Orchestration",
      subtitle: "Durable Execution & Fault Tolerance",
      icon: <Workflow className="w-6 h-6 text-emerald-400" />,
      steps: [
        "1. Deterministic State: Workflow code preserves state across crashes & deployments.",
        "2. Automatic Retries: Exponential backoff handles external API rate limits automatically.",
        "3. Long-Running Signals: Workflows can wait hours or days for human approval signals.",
        "4. Full Audit History: Replay events to debug exactly what happened at any millisecond."
      ]
    }
  }[topic];

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-[#1e282c] border border-gray-700 text-white rounded-xl shadow-2xl max-w-md w-full p-6 relative">
        <div className="flex justify-between items-center pb-3 border-b border-gray-700">
          <div className="flex items-center space-x-3">
            <div className="p-2 bg-gray-800 rounded-lg border border-gray-700">{content.icon}</div>
            <div>
              <h3 className="font-bold text-white text-base">{content.title}</h3>
              <p className="text-xs text-gray-400">{content.subtitle}</p>
            </div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-white p-1">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="py-4 space-y-3">
          {content.steps.map((st, idx) => (
            <div key={idx} className="p-3 bg-gray-800/60 rounded border border-gray-700/50 text-xs text-gray-300">
              {st}
            </div>
          ))}
        </div>

        <div className="pt-3 border-t border-gray-700 flex justify-end">
          <button
            onClick={onClose}
            className="px-4 py-1.5 bg-[#3c8dbc] hover:bg-[#367fa9] text-white text-xs font-semibold rounded"
          >
            Got it
          </button>
        </div>
      </div>
    </div>
  );
}
