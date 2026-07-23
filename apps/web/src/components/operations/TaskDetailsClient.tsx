"use client";

import React from 'react';
import ExecutionTraceViewer from '@/components/operations/ExecutionTraceViewer';
import { ShieldCheck, Calendar, CheckSquare, XSquare } from 'lucide-react';
import { useApiClient } from '@/lib/api-client';

export default function TaskDetailsClient({ taskId }: { taskId: string }) {
  const mockToken = "demo-jwt-token";
  const api = useApiClient();

  const handleDecision = async (decision: "APPROVED" | "REJECTED") => {
    try {
      await api.post(`/approvals/${taskId}/decision`, { decision });
      alert(`Decision ${decision} sent!`);
    } catch (err) {
      console.error(err);
      alert("Failed to send decision. See console.");
    }
  };

  return (
    <div className="max-w-5xl mx-auto py-8">
      <div className="flex justify-between items-end mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Task Execution Details</h1>
          <p className="text-gray-500 mt-1">ID: {taskId}</p>
        </div>
        <div className="flex space-x-3">
          <button 
            onClick={() => handleDecision("APPROVED")}
            className="inline-flex items-center px-4 py-2 bg-green-600 hover:bg-green-700 text-white text-sm font-medium rounded shadow-sm transition-colors"
          >
            <CheckSquare className="w-4 h-4 mr-2" /> Approve Action
          </button>
          <button 
            onClick={() => handleDecision("REJECTED")}
            className="inline-flex items-center px-4 py-2 bg-red-600 hover:bg-red-700 text-white text-sm font-medium rounded shadow-sm transition-colors"
          >
            <XSquare className="w-4 h-4 mr-2" /> Reject
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="md:col-span-2">
          {/* Timeline Component */}
          <ExecutionTraceViewer 
            taskId={taskId} 
            token={mockToken} 
          />
        </div>
        
        <div className="space-y-6">
          {/* Metadata Card */}
          <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-5">
            <h3 className="font-semibold text-gray-800 mb-4 border-b pb-2">Context</h3>
            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-500">Source</span>
                <span className="font-medium text-gray-900">Webhook (CRM)</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Workflow</span>
                <span className="font-medium text-gray-900">SalesPipeline</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Started</span>
                <span className="font-medium text-gray-900 flex items-center">
                  <Calendar className="w-3 h-3 mr-1" /> Just now
                </span>
              </div>
            </div>
          </div>

          {/* Security Card */}
          <div className="bg-[#222d32] text-white rounded-lg shadow-sm p-5 relative overflow-hidden">
            <ShieldCheck className="absolute -right-4 -top-4 w-24 h-24 text-white opacity-10" />
            <h3 className="font-semibold text-blue-300 mb-2 relative z-10">Zero Trust Audit</h3>
            <p className="text-xs text-gray-300 relative z-10 leading-relaxed">
              All tools execute in an isolated sandbox. Outputs are validated via Pydantic V2 before proceeding. 
              Any external network calls are routed through the SSRF-protected proxy.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
