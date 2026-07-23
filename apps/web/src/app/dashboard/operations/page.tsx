"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import Link from 'next/link';
import { Cpu, Play, CheckCircle2, ArrowRight } from 'lucide-react';

const agents = [
  { name: 'SalesSDR', role: 'Outreach & Lead Prospecting', status: 'Running', tasksToday: 42, tokens: '142.5k', color: 'border-blue-500' },
  { name: 'ResearchBot', role: 'Company & Tech Stack Scraper', status: 'Running', tasksToday: 28, tokens: '89.2k', color: 'border-purple-500' },
  { name: 'SupportAgent', role: 'Ticket Resolution & RAG Search', status: 'Running', tasksToday: 19, tokens: '45.1k', color: 'border-green-500' },
  { name: 'BIEngineer', role: 'Financial & Metrics Analytics', status: 'Idle', tasksToday: 15, tokens: '32.8k', color: 'border-amber-500' },
];

export default function OperationsPage() {
  return (
    <DashboardLayout>
      <div className="p-6 space-y-6 text-gray-800">
        <div className="flex items-center justify-between border-b pb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center">
              <Cpu className="w-6 h-6 mr-2 text-[#3c8dbc]" />
              AI Agent Operations & Multi-Agent Telemetry
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Monitor active LangGraph agent nodes, token consumption, and execution state traces.
            </p>
          </div>
          <span className="bg-purple-100 text-purple-800 text-xs font-semibold px-3 py-1 rounded-full border border-purple-200 flex items-center">
            <Play className="w-3.5 h-3.5 mr-1 text-purple-600" /> 4 Active Agents
          </span>
        </div>

        {/* Agent Grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          {agents.map(agent => (
            <div key={agent.name} className={`bg-white p-5 rounded-lg border-l-4 ${agent.color} border-y border-r border-gray-200 shadow-sm space-y-3`}>
              <div className="flex justify-between items-start">
                <div>
                  <h3 className="font-bold text-lg text-gray-900">{agent.name}</h3>
                  <p className="text-xs text-gray-500">{agent.role}</p>
                </div>
                <span className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
                  agent.status === 'Running' ? 'bg-green-100 text-green-700 border-green-200' : 'bg-gray-100 text-gray-600 border-gray-200'
                }`}>
                  {agent.status}
                </span>
              </div>
              <div className="pt-2 border-t flex justify-between text-xs text-gray-600">
                <span>Tasks Today: <b>{agent.tasksToday}</b></span>
                <span>Tokens: <b>{agent.tokens}</b></span>
              </div>
            </div>
          ))}
        </div>

        {/* Sample Execution Trace Quick Links */}
        <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm space-y-3">
          <h3 className="font-bold text-gray-900 text-base flex items-center">
            <CheckCircle2 className="w-4 h-4 mr-2 text-green-600" />
            Inspect Active Execution Traces
          </h3>
          <div className="flex flex-wrap gap-3">
            <Link 
              href="/dashboard/operations/demo-task-1"
              className="px-4 py-2 bg-blue-50 hover:bg-blue-100 text-blue-700 text-xs font-semibold rounded border border-blue-200 flex items-center transition-colors"
            >
              Task #demo-task-1 (Acme Corp Qualification) <ArrowRight className="w-3.5 h-3.5 ml-1.5" />
            </Link>
            <Link 
              href="/dashboard/operations/demo-task-2"
              className="px-4 py-2 bg-purple-50 hover:bg-purple-100 text-purple-700 text-xs font-semibold rounded border border-purple-200 flex items-center transition-colors"
            >
              Task #demo-task-2 (Wire Transfer HITL Review) <ArrowRight className="w-3.5 h-3.5 ml-1.5" />
            </Link>
          </div>
        </div>
      </div>
    </DashboardLayout>
  );
}
