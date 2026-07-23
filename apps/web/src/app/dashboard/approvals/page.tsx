"use client";

import React, { useState } from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { CheckSquare, ShieldAlert, CheckCircle2, XCircle, Clock } from 'lucide-react';

const initialApprovals = [
  { id: 'app-001', action: 'Wire Transfer $45,000 to Vendor B', requester: 'FinanceBot', riskScore: 'HIGH', reason: 'Threshold exceeded ($10,000 limit)', timestamp: '10 mins ago', status: 'PENDING' },
  { id: 'app-002', action: 'Delete Database Table temp_leads_2025', requester: 'DataCleanerBot', riskScore: 'CRITICAL', reason: 'Destructive DDL query detected', timestamp: '25 mins ago', status: 'PENDING' },
  { id: 'app-003', action: 'Send 500 Outbound Cold Emails', requester: 'SalesSDR', riskScore: 'MEDIUM', reason: 'Bulk message rate limit check', timestamp: '1 hour ago', status: 'PENDING' },
];

export default function ApprovalsPage() {
  const [approvals, setApprovals] = useState(initialApprovals);

  const handleAction = (id: string, newStatus: 'APPROVED' | 'REJECTED') => {
    setApprovals(prev => prev.map(a => a.id === id ? { ...a, status: newStatus } : a));
  };

  return (
    <DashboardLayout>
      <div className="p-6 space-y-6 text-gray-800">
        <div className="flex items-center justify-between border-b pb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center">
              <CheckSquare className="w-6 h-6 mr-2 text-[#3c8dbc]" />
              Human-in-the-Loop (HITL) Approval Center
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Review and approve high-risk AI agent action requests requiring human governance.
            </p>
          </div>
          <span className="bg-amber-100 text-amber-800 text-xs font-semibold px-3 py-1 rounded-full border border-amber-200 flex items-center">
            <Clock className="w-3.5 h-3.5 mr-1" /> {approvals.filter(a => a.status === 'PENDING').length} Pending Actions
          </span>
        </div>

        <div className="space-y-4">
          {approvals.map(item => (
            <div key={item.id} className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm flex flex-col md:flex-row md:items-center justify-between gap-4">
              <div className="space-y-1">
                <div className="flex items-center space-x-2">
                  <span className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
                    item.riskScore === 'CRITICAL' ? 'bg-red-100 text-red-700 border-red-200' :
                    item.riskScore === 'HIGH' ? 'bg-amber-100 text-amber-700 border-amber-200' : 'bg-blue-100 text-blue-700 border-blue-200'
                  }`}>
                    {item.riskScore} RISK
                  </span>
                  <span className="text-xs text-gray-400 font-mono">{item.id}</span>
                  <span className="text-xs text-gray-500">by <b>{item.requester}</b></span>
                </div>
                <h3 className="font-bold text-base text-gray-900">{item.action}</h3>
                <p className="text-xs text-gray-500 flex items-center">
                  <ShieldAlert className="w-3.5 h-3.5 mr-1 text-amber-500" />
                  {item.reason} • <span className="ml-1 text-gray-400">{item.timestamp}</span>
                </p>
              </div>

              <div className="flex items-center space-x-2 shrink-0">
                {item.status === 'PENDING' ? (
                  <>
                    <button
                      onClick={() => handleAction(item.id, 'APPROVED')}
                      className="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 rounded text-xs font-semibold flex items-center transition-colors shadow-sm"
                    >
                      <CheckCircle2 className="w-4 h-4 mr-1.5" /> Approve
                    </button>
                    <button
                      onClick={() => handleAction(item.id, 'REJECTED')}
                      className="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded text-xs font-semibold flex items-center transition-colors shadow-sm"
                    >
                      <XCircle className="w-4 h-4 mr-1.5" /> Reject
                    </button>
                  </>
                ) : (
                  <span className={`px-3 py-1 rounded text-xs font-bold border ${
                    item.status === 'APPROVED' ? 'bg-green-100 text-green-700 border-green-200' : 'bg-red-100 text-red-700 border-red-200'
                  }`}>
                    {item.status}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </DashboardLayout>
  );
}
