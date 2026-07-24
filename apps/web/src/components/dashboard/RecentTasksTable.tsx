"use client";

import React from 'react';
import Link from 'next/link';
import { ArrowUpRight, Cpu, Clock, CheckCircle2, AlertCircle } from 'lucide-react';

const mockTasks = [
  { id: 'demo-task-1', title: 'Qualify & Enrich Inbound Lead: Acme Corp', agent: 'SalesSDR', status: 'COMPLETED', time: '5 mins ago', duration: '1.2s' },
  { id: 'demo-task-2', title: 'Wire Transfer HITL Governance Check ($45k)', agent: 'FinanceBot', status: 'PENDING_APPROVAL', time: '12 mins ago', duration: '0.4s' },
  { id: 'demo-task-3', title: 'RAG Knowledge Indexing: 15 PDF Documents', agent: 'KnowledgeAssistant', status: 'RUNNING', time: '18 mins ago', duration: '4.8s' },
  { id: 'demo-task-4', title: 'Zero-Trust Network Intercept Diagnostics', agent: 'SecOpsBot', status: 'COMPLETED', time: '45 mins ago', duration: '0.9s' },
  { id: 'demo-task-5', title: 'Weekly Revenue & Pipeline Analytics Sync', agent: 'BIEngineer', status: 'FAILED', time: '1 hour ago', duration: '2.1s' },
];

export default function RecentTasksTable() {
  const getBadgeStyle = (status: string) => {
    switch (status) {
      case 'COMPLETED': return 'bg-emerald-50 text-emerald-700 border-emerald-200';
      case 'RUNNING': return 'bg-blue-50 text-blue-700 border-blue-200 animate-pulse';
      case 'PENDING_APPROVAL': return 'bg-amber-50 text-amber-700 border-amber-200';
      case 'FAILED': return 'bg-rose-50 text-rose-700 border-rose-200';
      default: return 'bg-slate-50 text-slate-700 border-slate-200';
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'COMPLETED': return <CheckCircle2 className="w-3.5 h-3.5 mr-1 text-emerald-600" />;
      case 'RUNNING': return <Clock className="w-3.5 h-3.5 mr-1 text-blue-600 animate-spin" />;
      case 'PENDING_APPROVAL': return <AlertCircle className="w-3.5 h-3.5 mr-1 text-amber-600" />;
      case 'FAILED': return <AlertCircle className="w-3.5 h-3.5 mr-1 text-rose-600" />;
      default: return null;
    }
  };

  return (
    <div className="bg-white rounded-xl border border-slate-200/80 shadow-xs overflow-hidden">
      <div className="px-6 py-4 border-b border-slate-100 flex justify-between items-center bg-slate-50/50">
        <div>
          <h3 className="font-bold text-slate-900 text-sm">Active Agent Execution Traces</h3>
          <p className="text-xs text-slate-500 mt-0.5">Real-time task dispatch log across multi-agent nodes</p>
        </div>
        <Link href="/dashboard/operations" className="text-xs font-semibold text-blue-600 hover:text-blue-700 flex items-center transition-colors">
          View All Traces <ArrowUpRight className="h-3.5 w-3.5 ml-1" />
        </Link>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs text-left">
          <thead className="bg-slate-50/80 text-slate-500 font-semibold border-b border-slate-100 uppercase tracking-wider">
            <tr>
              <th className="px-6 py-3">Trace ID</th>
              <th className="px-6 py-3">Task Workflow</th>
              <th className="px-6 py-3">Assigned Agent</th>
              <th className="px-6 py-3">Status</th>
              <th className="px-6 py-3">Duration</th>
              <th className="px-6 py-3">Timestamp</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {mockTasks.map((task) => (
              <tr key={task.id} className="hover:bg-slate-50/80 transition-colors group">
                <td className="px-6 py-3.5 font-mono font-medium text-blue-600">
                  <Link href={`/dashboard/operations/${task.id}`} className="hover:underline">
                    #{task.id}
                  </Link>
                </td>
                <td className="px-6 py-3.5 text-slate-900 font-semibold max-w-xs truncate">{task.title}</td>
                <td className="px-6 py-3.5 text-slate-600">
                  <span className="inline-flex items-center px-2 py-0.5 rounded bg-slate-100 border border-slate-200 text-slate-700 font-medium">
                    <Cpu className="w-3 h-3 mr-1 text-slate-500" />
                    {task.agent}
                  </span>
                </td>
                <td className="px-6 py-3.5">
                  <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full font-semibold border ${getBadgeStyle(task.status)}`}>
                    {getStatusIcon(task.status)}
                    {task.status.replace('_', ' ')}
                  </span>
                </td>
                <td className="px-6 py-3.5 text-slate-500 font-mono">{task.duration}</td>
                <td className="px-6 py-3.5 text-slate-400 font-medium">{task.time}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
