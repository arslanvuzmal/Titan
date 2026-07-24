"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { Plug, CheckCircle2, RefreshCw } from 'lucide-react';

export default function IntegrationsPage() {
  const integrations = [
    { name: 'Temporal Cloud', category: 'Orchestration Engine', status: 'Connected', ping: '12ms', color: 'border-purple-500' },
    { name: 'OpenAI API (GPT-4o)', category: 'LLM Reasoning Provider', status: 'Connected', ping: '145ms', color: 'border-emerald-500' },
    { name: 'Qdrant Vector DB', category: 'Vector Database', status: 'Connected', ping: '8ms', color: 'border-blue-500' },
    { name: 'PostgreSQL 16', category: 'Relational Database', status: 'Connected', ping: '3ms', color: 'border-indigo-500' },
    { name: 'SendGrid Email API', category: 'Outreach & Messaging', status: 'Connected', ping: '65ms', color: 'border-amber-500' },
    { name: 'Slack Webhook', category: 'Alerts & Governance', status: 'Connected', ping: '32ms', color: 'border-pink-500' },
  ];

  return (
    <DashboardLayout>
      <div className="space-y-6 animate-fade-in">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-slate-200/80">
          <div>
            <h1 className="text-2xl font-bold text-slate-900 tracking-tight flex items-center">
              <Plug className="w-6 h-6 mr-2 text-blue-600" />
              Integrations & Connected Infrastructure
            </h1>
            <p className="text-xs text-slate-500 mt-1">Live status check across external LLM APIs, vector stores, and Temporal workers</p>
          </div>
          <button className="px-3.5 py-2 bg-white border border-slate-200 rounded-lg text-xs font-semibold text-slate-700 hover:bg-slate-50 transition-colors shadow-xs flex items-center">
            <RefreshCw className="w-3.5 h-3.5 mr-1.5 text-slate-500" /> Test Connections
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
          {integrations.map((item) => (
            <div key={item.name} className={`bg-white p-5 rounded-xl border-l-4 ${item.color} border-y border-r border-slate-200/80 shadow-xs space-y-3`}>
              <div className="flex justify-between items-start">
                <div>
                  <h3 className="font-bold text-base text-slate-900">{item.name}</h3>
                  <p className="text-xs text-slate-500">{item.category}</p>
                </div>
                <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">
                  <CheckCircle2 className="w-3 h-3 mr-1" /> {item.status}
                </span>
              </div>
              <div className="pt-3 border-t border-slate-100 flex justify-between text-xs text-slate-500">
                <span>Health Ping: <b className="text-slate-700 font-mono">{item.ping}</b></span>
                <span className="text-emerald-600 font-medium">Active</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </DashboardLayout>
  );
}
