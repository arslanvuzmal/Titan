"use client";

import React, { useState } from 'react';
import { User, Check, X, Clock } from 'lucide-react';

const initialApprovals = [
  { id: 'wf-9128', agent: 'FinanceBot', action: 'Execute Wire Transfer - $45,000 to Vendor X', time: '10m ago', risk: 'High' },
  { id: 'wf-3341', agent: 'SalesAgent', action: 'Send Bulk Email Campaign (15,000 recipients)', time: '45m ago', risk: 'Medium' },
  { id: 'wf-1120', agent: 'HR-Assistant', action: 'Finalize Offer Letter for Candidate Y', time: '2h ago', risk: 'Low' },
];

export default function HitlQueue() {
  const [approvals, setApprovals] = useState(initialApprovals);

  const handleApprove = (id: string) => {
    setApprovals(approvals.filter(a => a.id !== id));
  };

  const handleReject = (id: string) => {
    setApprovals(approvals.filter(a => a.id !== id));
  };

  return (
    <div className="bg-white rounded shadow-sm">
      <div className="px-4 py-3 border-b border-gray-100 flex justify-between items-center">
        <h3 className="font-medium text-gray-800 flex items-center">
          <User size={18} className="mr-2 text-[#f39c12]" />
          Human-in-the-Loop Queue
        </h3>
        {approvals.length > 0 && (
          <span className="bg-[#f39c12] text-white text-xs font-bold px-2 py-0.5 rounded-full">
            {approvals.length} Pending
          </span>
        )}
      </div>
      
      <div className="p-0">
        {approvals.length === 0 ? (
          <div className="p-8 flex flex-col items-center justify-center text-gray-500">
            <Check size={48} className="text-green-300 mb-2" />
            <p className="text-sm font-medium text-gray-600">All caught up!</p>
            <p className="text-xs mt-1">No pending approvals in the queue.</p>
          </div>
        ) : (
          <ul className="divide-y divide-gray-100">
            {approvals.map((item) => (
              <li key={item.id} className="p-4 hover:bg-gray-50 transition-colors">
                <div className="flex justify-between items-start mb-2">
                  <div className="flex items-center">
                    <span className="text-xs font-mono text-gray-500 bg-gray-100 px-1.5 py-0.5 rounded mr-2">
                      {item.id}
                    </span>
                    <span className="text-sm font-semibold text-gray-800">{item.agent}</span>
                  </div>
                  <div className="flex items-center text-xs text-gray-500">
                    <Clock size={12} className="mr-1" />
                    {item.time}
                  </div>
                </div>
                
                <p className="text-sm text-gray-600 mb-3 ml-1 border-l-2 border-[#3c8dbc] pl-2">
                  {item.action}
                </p>
                
                <div className="flex justify-between items-center mt-2">
                  <span className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded-full ${
                    item.risk === 'High' ? 'bg-red-100 text-red-700' : 
                    item.risk === 'Medium' ? 'bg-orange-100 text-orange-700' : 
                    'bg-green-100 text-green-700'
                  }`}>
                    {item.risk} RISK
                  </span>
                  
                  <div className="flex space-x-2">
                    <button 
                      onClick={() => handleReject(item.id)}
                      className="flex items-center text-xs font-medium text-gray-600 bg-white border border-gray-300 hover:bg-gray-50 px-3 py-1.5 rounded transition-colors"
                    >
                      <X size={14} className="mr-1 text-red-500" />
                      Reject
                    </button>
                    <button 
                      onClick={() => handleApprove(item.id)}
                      className="flex items-center text-xs font-medium text-white bg-[#00a65a] hover:bg-[#008d4c] px-3 py-1.5 rounded transition-colors shadow-sm"
                    >
                      <Check size={14} className="mr-1" />
                      Approve
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
