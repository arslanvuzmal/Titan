"use client";

import React from 'react';
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer, Legend } from 'recharts';

const data = [
  { name: 'Finance Agent', value: 35, color: '#3c8dbc' },
  { name: 'Sales Agent', value: 25, color: '#00a65a' },
  { name: 'HR Assistant', value: 15, color: '#f39c12' },
  { name: 'Intel Scraper', value: 25, color: '#dd4b39' },
];

export default function AgentPieChart() {
  return (
    <div className="bg-white rounded shadow-sm p-4 flex flex-col h-full min-h-[350px]">
      <h3 className="text-lg font-medium text-gray-800 mb-2">Agent Execution Distribution</h3>
      <p className="text-sm text-gray-500 mb-4">Task volume by LangGraph agent</p>
      
      <div className="flex-1 w-full h-full min-h-[250px]">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={data}
              cx="50%"
              cy="50%"
              innerRadius={60}
              outerRadius={80}
              paddingAngle={5}
              dataKey="value"
            >
              {data.map((entry, index) => (
                <Cell key={`cell-${index}`} fill={entry.color} />
              ))}
            </Pie>
            <Tooltip 
              contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1)' }}
              itemStyle={{ color: '#374151', fontWeight: 500 }}
            />
            <Legend verticalAlign="bottom" height={36} iconType="circle" />
          </PieChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
