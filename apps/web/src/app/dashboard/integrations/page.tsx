"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { Link as LinkIcon, CheckCircle2, ShieldCheck, Zap } from 'lucide-react';

const integrations = [
  { id: 'int-1', name: 'Temporal.io Cluster', category: 'Workflow Engine', status: 'Connected', latency: '18ms', iconColor: 'text-purple-600' },
  { id: 'int-2', name: 'OpenAI GPT-4o API', category: 'LLM Provider', status: 'Connected', latency: '320ms', iconColor: 'text-green-600' },
  { id: 'int-3', name: 'PostgreSQL Database', category: 'Primary Storage', status: 'Connected', latency: '4ms', iconColor: 'text-blue-600' },
  { id: 'int-4', name: 'Qdrant Vector DB', category: 'Embeddings', status: 'Connected', latency: '12ms', iconColor: 'text-indigo-600' },
  { id: 'int-5', name: 'HubSpot CRM Webhook', category: 'CRM Sync', status: 'Connected', latency: '85ms', iconColor: 'text-orange-600' },
  { id: 'int-6', name: 'SendGrid Email API', category: 'Communications', status: 'Connected', latency: '110ms', iconColor: 'text-[#3c8dbc]' },
];

export default function IntegrationsPage() {
  return (
    <DashboardLayout>
      <div className="p-6 space-y-6 text-gray-800">
        <div className="flex items-center justify-between border-b pb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center">
              <LinkIcon className="w-6 h-6 mr-2 text-[#3c8dbc]" />
              Enterprise Integrations & Connectors
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Active connections powering TITAN OS agents, storage engines, and communications.
            </p>
          </div>
          <span className="bg-emerald-100 text-emerald-800 text-xs font-semibold px-3 py-1 rounded-full border border-emerald-200 flex items-center">
            <CheckCircle2 className="w-3.5 h-3.5 mr-1" /> All Systems Nominal
          </span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {integrations.map(item => (
            <div key={item.id} className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm space-y-3 hover:border-[#3c8dbc] transition-colors">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-gray-400 uppercase tracking-wide">{item.category}</span>
                <span className="text-[10px] font-bold px-2 py-0.5 rounded bg-green-100 text-green-700 border border-green-200 flex items-center">
                  <span className="w-1.5 h-1.5 rounded-full bg-green-500 mr-1 animate-pulse" />
                  {item.status}
                </span>
              </div>
              <h3 className={`font-bold text-lg text-gray-900 flex items-center ${item.iconColor}`}>
                <Zap className="w-5 h-5 mr-2" />
                {item.name}
              </h3>
              <div className="pt-2 border-t flex justify-between text-xs text-gray-500">
                <span>Latency: <b>{item.latency}</b></span>
                <span className="flex items-center text-emerald-600 font-semibold">
                  <ShieldCheck className="w-3.5 h-3.5 mr-1" /> Health 100%
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </DashboardLayout>
  );
}
