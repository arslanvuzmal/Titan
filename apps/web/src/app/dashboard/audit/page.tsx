"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { ShieldCheck, Lock, AlertTriangle, CheckCircle2 } from 'lucide-react';

const auditLogs = [
  { id: 'aud-101', event: 'SSRF Check Intercept', source: 'Security Proxy', status: 'BLOCKED', risk: 'HIGH', timestamp: '2 mins ago', details: 'Blocked query to 169.254.169.254 (AWS IMDS)' },
  { id: 'aud-102', event: 'HITL Wire Transfer Approved', source: 'Admin User', status: 'APPROVED', risk: 'HIGH', timestamp: '15 mins ago', details: 'Approved $45,000 wire transfer for TSK-1028' },
  { id: 'aud-103', event: 'Pydantic Schema Validation', source: 'FastAPI Gateway', status: 'PASSED', risk: 'LOW', timestamp: '24 mins ago', details: 'Validated inbound webhook json payload (100% fit)' },
  { id: 'aud-104', event: 'Temporal State Checkpoint', source: 'Temporal Engine', status: 'SAVED', risk: 'LOW', timestamp: '40 mins ago', details: 'Durable execution state persisted for SalesSDR' },
];

export default function AuditLogsPage() {
  return (
    <DashboardLayout>
      <div className="p-6 space-y-6 text-gray-800">
        <div className="flex items-center justify-between border-b pb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center">
              <ShieldCheck className="w-6 h-6 mr-2 text-[#3c8dbc]" />
              Zero-Trust Audit Logs & Security Telemetry
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Immutable ledger of agent tool invocations, SSRF interceptions, and human approval events.
            </p>
          </div>
          <span className="bg-blue-100 text-blue-800 text-xs font-semibold px-3 py-1 rounded-full border border-blue-200 flex items-center">
            <Lock className="w-3.5 h-3.5 mr-1" /> Zero-Trust Active
          </span>
        </div>

        <div className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden">
          <table className="w-full text-sm text-left">
            <thead className="bg-gray-50 text-xs font-semibold text-gray-500 border-b">
              <tr>
                <th className="px-4 py-3">Audit ID</th>
                <th className="px-4 py-3">Security Event</th>
                <th className="px-4 py-3">Source Engine</th>
                <th className="px-4 py-3">Risk Level</th>
                <th className="px-4 py-3">Outcome</th>
                <th className="px-4 py-3">Details</th>
                <th className="px-4 py-3">Timestamp</th>
              </tr>
            </thead>
            <tbody className="divide-y text-gray-700">
              {auditLogs.map(log => (
                <tr key={log.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3 font-mono text-xs text-gray-500">{log.id}</td>
                  <td className="px-4 py-3 font-bold text-gray-900">{log.event}</td>
                  <td className="px-4 py-3 text-gray-600">{log.source}</td>
                  <td className="px-4 py-3">
                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
                      log.risk === 'HIGH' ? 'bg-red-100 text-red-700 border-red-200' : 'bg-green-100 text-green-700 border-green-200'
                    }`}>
                      {log.risk}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-semibold">
                    {log.status === 'BLOCKED' ? (
                      <span className="text-red-600 flex items-center"><AlertTriangle className="w-3.5 h-3.5 mr-1" /> BLOCKED</span>
                    ) : (
                      <span className="text-green-600 flex items-center"><CheckCircle2 className="w-3.5 h-3.5 mr-1" /> {log.status}</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500 max-w-xs truncate">{log.details}</td>
                  <td className="px-4 py-3 text-xs text-gray-400 font-mono">{log.timestamp}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </DashboardLayout>
  );
}
