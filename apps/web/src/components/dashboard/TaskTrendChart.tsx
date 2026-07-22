"use client";

import React from 'react';
import { 
  LineChart, 
  Line, 
  XAxis, 
  YAxis, 
  CartesianGrid, 
  Tooltip, 
  ResponsiveContainer 
} from 'recharts';

const mockData = [
  { name: 'Mon', tasks: 40, errors: 2 },
  { name: 'Tue', tasks: 30, errors: 1 },
  { name: 'Wed', tasks: 45, errors: 4 },
  { name: 'Thu', tasks: 50, errors: 3 },
  { name: 'Fri', tasks: 60, errors: 2 },
  { name: 'Sat', tasks: 20, errors: 0 },
  { name: 'Sun', tasks: 15, errors: 0 },
];

export default function TaskTrendChart() {
  return (
    <div className="bg-white p-4 rounded shadow-sm">
      <div className="mb-4">
        <h3 className="text-lg font-medium text-gray-800">Task Execution Trends</h3>
        <p className="text-sm text-gray-500">Last 7 days</p>
      </div>
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={mockData} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e5e7eb" />
            <XAxis dataKey="name" axisLine={false} tickLine={false} tick={{ fontSize: 12, fill: '#6b7280' }} />
            <YAxis axisLine={false} tickLine={false} tick={{ fontSize: 12, fill: '#6b7280' }} />
            <Tooltip 
              contentStyle={{ borderRadius: '4px', border: 'none', boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1)' }}
            />
            <Line type="monotone" dataKey="tasks" stroke="#3c8dbc" strokeWidth={3} dot={{ r: 4 }} activeDot={{ r: 6 }} />
            <Line type="monotone" dataKey="errors" stroke="#dd4b39" strokeWidth={2} dot={{ r: 3 }} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
