"use client";

import React from 'react';

const mockTasks = [
  { id: 'TSK-1029', title: 'Generate Q3 Financial Report', agent: 'FinanceBot', status: 'COMPLETED', time: '10 mins ago' },
  { id: 'TSK-1030', title: 'Qualify Inbound Lead: Acme Corp', agent: 'SalesSDR', status: 'RUNNING', time: 'Just now' },
  { id: 'TSK-1031', title: 'Update HR Policy Knowledgebase', agent: 'HR-Assistant', status: 'PENDING_APPROVAL', time: '1 hour ago' },
  { id: 'TSK-1032', title: 'Scrape Competitor Pricing', agent: 'MarketIntel', status: 'FAILED', time: '2 hours ago' },
  { id: 'TSK-1033', title: 'Draft Marketing Email Sequence', agent: 'Copywriter', status: 'COMPLETED', time: '3 hours ago' },
];

export default function RecentTasksTable() {
  const getBadgeStyle = (status: string) => {
    switch (status) {
      case 'COMPLETED': return 'bg-green-100 text-green-700 border-green-200';
      case 'RUNNING': return 'bg-blue-100 text-blue-700 border-blue-200 animate-pulse';
      case 'PENDING_APPROVAL': return 'bg-orange-100 text-orange-700 border-orange-200';
      case 'FAILED': return 'bg-red-100 text-red-700 border-red-200';
      default: return 'bg-gray-100 text-gray-700 border-gray-200';
    }
  };

  return (
    <div className="bg-white rounded shadow-sm overflow-hidden h-full">
      <div className="px-4 py-3 border-b border-gray-100 flex justify-between items-center bg-gray-50/50">
        <h3 className="font-medium text-gray-800">Recent Workflow Executions</h3>
        <button className="text-xs font-medium text-[#3c8dbc] hover:text-[#2c6ea0] transition-colors">View All &rarr;</button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead className="bg-white text-gray-500 font-medium border-b border-gray-100 text-xs uppercase tracking-wider">
            <tr>
              <th className="px-4 py-3 font-medium">Execution ID</th>
              <th className="px-4 py-3 font-medium">Task</th>
              <th className="px-4 py-3 font-medium">Agent Node</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 font-medium">Timestamp</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 bg-white">
            {mockTasks.map((task) => (
              <tr key={task.id} className="hover:bg-blue-50/30 transition-colors group">
                <td className="px-4 py-3 font-mono text-xs font-medium text-[#3c8dbc] cursor-pointer group-hover:underline">
                  {task.id}
                </td>
                <td className="px-4 py-3 text-gray-800 font-medium">{task.title}</td>
                <td className="px-4 py-3 text-gray-600">
                  <span className="flex items-center">
                    <span className="w-2 h-2 rounded-full bg-gray-300 mr-2"></span>
                    {task.agent}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <span className={`px-2.5 py-1 rounded-full text-[10px] font-bold uppercase tracking-wide border ${getBadgeStyle(task.status)}`}>
                    {task.status.replace('_', ' ')}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-500 text-xs">{task.time}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
