"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { ShieldCheck, ShieldAlert, CheckCircle2, Lock } from 'lucide-react';

export default function AuditPage() {
  const auditLogs = [
    { id: 'AUD-901', action: 'SSRF URL Validation Check Pass', target: 'https://api.github.com/repos', severity: 'LOW', actor: 'SSRFGuard', timestamp: '2 mins ago' },
    { id: 'AUD-902', action: 'Human Approval Granted: Wire Transfer $45k', target: 'FinanceBot / Action #app-001', severity: 'HIGH', actor: 'John Doe (Admin)', timestamp: '15 mins ago' },
    { id: 'AUD-903', action: 'Blocked Internal IP Outbound Intercept', target: 'http://169.254.169.254/latest/meta-data', severity: 'CRITICAL', actor: 'SSRFGuard', timestamp: '1 hour ago' },
    { id: 'AUD-904', action: 'LLM Key Rotation & Policy Update', target: 'Settings / OpenAI Provider', severity: 'MEDIUM', actor: 'System Auto', timestamp: '3 hours ago' },
  ];

  return (
    <DashboardLayout>
      <div className="space-y-6 animate-fade-in">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-slate-200/80">
          <div>
            <h1 className="text-2xl font-bold text-slate-900 tracking-tight flex items-center">
              <ShieldCheck className="w-6 h-6 mr-2 text-blue-600" />
              Zero-Trust Audit Logs & SSRF Defense Intercepts
            </h1>
            <p className="text-xs text-slate-500 mt-1">Immutable security log of tool executions, egress proxies, and HITL approvals</p>
          </div>
          <span className="inline-flex items-center px-3 py-1 rounded-full bg-emerald-50 text-emerald-800 text-xs font-semibold border border-emerald-200">
            <Lock className="w-3.5 h-3.5 mr-1" /> Egress Proxy Active
          </span>
        </div>

        <div className="bg-white rounded-xl border border-slate-200/80 shadow-xs overflow-hidden">
          <table className="w-full text-xs text-left">
            <thead className="bg-slate-50 text-slate-500 font-semibold border-b border-slate-100 uppercase tracking-wider">
              <tr>
                <th className="px-6 py-3">Log ID</th>
                <th className="px-6 py-3">Security Action</th>
                <th className="px-6 py-3">Target Resource</th>
                <th className="px-6 py-3">Severity</th>
                <th className="px-6 py-3">Actor / Bot</th>
                <th className="px-6 py-3">Timestamp</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {auditLogs.map((log) => (
                <tr key={log.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-6 py-3.5 font-mono font-medium text-slate-500">{log.id}</td>
                  <td className="px-6 py-3.5 text-slate-900 font-semibold">{log.action}</td>
                  <td className="px-6 py-3.5 text-slate-600 font-mono truncate max-w-xs">{log.target}</td>
                  <td className="px-6 py-3.5">
                    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full font-bold border ${
                      log.severity === 'CRITICAL' ? 'bg-rose-50 text-rose-700 border-rose-200' :
                      log.severity === 'HIGH' ? 'bg-amber-50 text-amber-700 border-amber-200' :
                      log.severity === 'MEDIUM' ? 'bg-blue-50 text-blue-700 border-blue-200' : 'bg-slate-50 text-slate-600 border-slate-200'
                    }`}>
                      {log.severity === 'CRITICAL' ? <ShieldAlert className="w-3 h-3 mr-1 text-rose-600" /> : <CheckCircle2 className="w-3 h-3 mr-1 text-emerald-600" />}
                      {log.severity}
                    </span>
                  </td>
                  <td className="px-6 py-3.5 text-slate-700 font-medium">{log.actor}</td>
                  <td className="px-6 py-3.5 text-slate-400">{log.timestamp}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </DashboardLayout>
  );
}
