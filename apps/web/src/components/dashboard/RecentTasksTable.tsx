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
  const getBadgeColor = (status: string) => {
    switch (status) {
      case 'COMPLETED': return 'bg-[#00a65a] text-white';
      case 'RUNNING': return 'bg-[#00c0ef] text-white';
      case 'PENDING_APPROVAL': return 'bg-[#f39c12] text-white';
      case 'FAILED': return 'bg-[#dd4b39] text-white';
      default: return 'bg-gray-200 text-gray-800';
    }
  };

  return (
    <div className="bg-white rounded shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-100 flex justify-between items-center">
        <h3 className="font-medium text-gray-800">Recent Tasks</h3>
        <button className="text-xs text-[#3c8dbc] hover:underline">View All</button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead className="bg-gray-50 text-gray-600 font-medium border-b border-gray-200">
            <tr>
              <th className="px-4 py-3">Task ID</th>
              <th className="px-4 py-3">Title</th>
              <th className="px-4 py-3">Agent</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {mockTasks.map((task) => (
              <tr key={task.id} className="hover:bg-gray-50 transition-colors">
                <td className="px-4 py-3 font-medium text-[#3c8dbc] cursor-pointer hover:underline">{task.id}</td>
                <td className="px-4 py-3 text-gray-800">{task.title}</td>
                <td className="px-4 py-3 text-gray-600">{task.agent}</td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-1 rounded text-xs font-semibold ${getBadgeColor(task.status)}`}>
                    {task.status}
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
