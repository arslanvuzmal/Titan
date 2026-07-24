"use client";

import React from 'react';
import { 
  AreaChart, 
  Area, 
  XAxis, 
  YAxis, 
  CartesianGrid, 
  Tooltip, 
  ResponsiveContainer 
} from 'recharts';
import { Activity } from 'lucide-react';

const mockData = [
  { name: 'Mon', executions: 420, activeAgents: 18, errors: 2 },
  { name: 'Tue', executions: 580, activeAgents: 22, errors: 4 },
  { name: 'Wed', executions: 890, activeAgents: 28, errors: 1 },
  { name: 'Thu', executions: 1120, activeAgents: 35, errors: 3 },
  { name: 'Fri', executions: 1450, activeAgents: 42, errors: 2 },
  { name: 'Sat', executions: 920, activeAgents: 25, errors: 0 },
  { name: 'Sun', executions: 740, activeAgents: 20, errors: 1 },
];

export default function TaskTrendChart() {
  return (
    <div className="bg-white p-6 rounded-xl border border-slate-200/80 shadow-xs space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-bold text-slate-900 flex items-center">
            <Activity className="h-4 w-4 mr-2 text-blue-600" />
            AI Execution & Agent Telemetry Volume
          </h3>
          <p className="text-xs text-slate-500 mt-0.5">Real-time daily agent workflow processing rate</p>
        </div>
        <div className="flex items-center space-x-3 text-xs">
          <span className="flex items-center text-slate-600 font-medium">
            <span className="w-2.5 h-2.5 rounded-full bg-blue-500 mr-1.5" /> Executions
          </span>
          <span className="flex items-center text-slate-600 font-medium">
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-500 mr-1.5" /> Active Agents
          </span>
        </div>
      </div>

      <div className="h-72 w-full pt-2">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={mockData} margin={{ top: 10, right: 10, bottom: 0, left: -20 }}>
            <defs>
              <linearGradient id="colorExec" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.0} />
              </linearGradient>
              <linearGradient id="colorAgents" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#10b981" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#10b981" stopOpacity={0.0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f1f5f9" />
            <XAxis dataKey="name" axisLine={false} tickLine={false} tick={{ fontSize: 12, fill: '#64748b' }} />
            <YAxis axisLine={false} tickLine={false} tick={{ fontSize: 12, fill: '#64748b' }} />
            <Tooltip 
              contentStyle={{ 
                backgroundColor: '#0f172a', 
                borderRadius: '8px', 
                border: 'none', 
                color: '#fff',
                fontSize: '12px',
                boxShadow: '0 10px 15px -3px rgba(0, 0, 0, 0.3)'
              }}
              itemStyle={{ color: '#e2e8f0' }}
            />
            <Area type="monotone" dataKey="executions" stroke="#3b82f6" strokeWidth={2.5} fillOpacity={1} fill="url(#colorExec)" />
            <Area type="monotone" dataKey="activeAgents" stroke="#10b981" strokeWidth={2} fillOpacity={1} fill="url(#colorAgents)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
