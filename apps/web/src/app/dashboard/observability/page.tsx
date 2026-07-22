"use client";

import React, { useEffect, useState } from "react";
import { LineChart, Line, BarChart, Bar, PieChart, Pie, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, Cell } from "recharts";
import { Activity, Server, Database, Clock, Zap } from "lucide-react";

// Mock Data
const tokenData = [
  { time: "00:00", tokens: 1200 },
  { time: "04:00", tokens: 2100 },
  { time: "08:00", tokens: 800 },
  { time: "12:00", tokens: 3400 },
  { time: "16:00", tokens: 2800 },
  { time: "20:00", tokens: 1500 },
];

const costData = [
  { model: "gpt-4o", cost: 12.5 },
  { model: "gpt-4-turbo", cost: 8.2 },
  { model: "claude-3-opus", cost: 5.0 },
  { model: "claude-3-sonnet", cost: 2.1 },
];

const toolData = [
  { name: "Success", value: 85, color: "#10b981" },
  { name: "Failure", value: 15, color: "#ef4444" },
];

export default function ObservabilityDashboard() {
  const [activeAgents, setActiveAgents] = useState(12);
  const [avgLatency, setAvgLatency] = useState(2.4);
  
  // Real-time mockup
  useEffect(() => {
    const interval = setInterval(() => {
      setActiveAgents(Math.floor(Math.random() * 5) + 10);
      setAvgLatency(Number((Math.random() * 1.5 + 1.5).toFixed(1)));
    }, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="p-8 max-w-7xl mx-auto space-y-8">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Observability & Monitoring</h1>
          <p className="text-gray-500 mt-2">Real-time telemetry, tracing, and system health.</p>
        </div>
        <div className="flex gap-4">
          <div className="px-4 py-2 bg-green-100 text-green-800 rounded-lg flex items-center font-medium">
            <div className="w-2 h-2 bg-green-500 rounded-full mr-2 animate-pulse"></div>
            System Operational
          </div>
        </div>
      </div>

      {/* Top Row: Key Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm flex items-center justify-between">
          <div>
            <p className="text-sm text-gray-500 font-medium">Active Agents</p>
            <p className="text-3xl font-bold mt-1 text-gray-900">{activeAgents}</p>
          </div>
          <div className="p-3 bg-blue-50 text-blue-600 rounded-lg">
            <Activity size={24} />
          </div>
        </div>
        
        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm flex items-center justify-between">
          <div>
            <p className="text-sm text-gray-500 font-medium">Avg Agent Latency</p>
            <p className="text-3xl font-bold mt-1 text-gray-900">{avgLatency}s</p>
          </div>
          <div className="p-3 bg-purple-50 text-purple-600 rounded-lg">
            <Clock size={24} />
          </div>
        </div>

        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm flex items-center justify-between">
          <div>
            <p className="text-sm text-gray-500 font-medium">24h Token Usage</p>
            <p className="text-3xl font-bold mt-1 text-gray-900">11.8k</p>
          </div>
          <div className="p-3 bg-orange-50 text-orange-600 rounded-lg">
            <Zap size={24} />
          </div>
        </div>

        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm flex items-center justify-between">
          <div>
            <p className="text-sm text-gray-500 font-medium">DB Connection Pool</p>
            <p className="text-3xl font-bold mt-1 text-gray-900">Healthy</p>
          </div>
          <div className="p-3 bg-green-50 text-green-600 rounded-lg">
            <Database size={24} />
          </div>
        </div>
      </div>

      {/* Middle Row: Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Token Usage Chart */}
        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm lg:col-span-2">
          <h2 className="text-lg font-bold text-gray-900 mb-6">LLM Token Consumption (24h)</h2>
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={tokenData}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e5e7eb" />
                <XAxis dataKey="time" stroke="#6b7280" fontSize={12} tickLine={false} axisLine={false} />
                <YAxis stroke="#6b7280" fontSize={12} tickLine={false} axisLine={false} tickFormatter={(value) => `${value / 1000}k`} />
                <Tooltip 
                  contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)' }}
                />
                <Line type="monotone" dataKey="tokens" stroke="#4f46e5" strokeWidth={3} dot={{ r: 4 }} activeDot={{ r: 6 }} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Tool Success Rate */}
        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm">
          <h2 className="text-lg font-bold text-gray-900 mb-6">Tool Success Rate</h2>
          <div className="h-72 flex items-center justify-center">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={toolData}
                  innerRadius={60}
                  outerRadius={80}
                  paddingAngle={5}
                  dataKey="value"
                >
                  {toolData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend verticalAlign="bottom" height={36} iconType="circle" />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      {/* Bottom Row: Cost and Traces */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Cost Analysis */}
        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm">
          <h2 className="text-lg font-bold text-gray-900 mb-6">Cost Breakdown by Model ($)</h2>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={costData} layout="vertical" margin={{ top: 0, right: 0, left: 40, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" horizontal={true} vertical={false} stroke="#e5e7eb" />
                <XAxis type="number" stroke="#6b7280" fontSize={12} tickLine={false} axisLine={false} />
                <YAxis dataKey="model" type="category" stroke="#6b7280" fontSize={12} tickLine={false} axisLine={false} />
                <Tooltip cursor={{ fill: '#f3f4f6' }} contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)' }} />
                <Bar dataKey="cost" fill="#0ea5e9" radius={[0, 4, 4, 0]} barSize={24} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Live Traces */}
        <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm flex flex-col">
          <div className="flex justify-between items-center mb-6">
            <h2 className="text-lg font-bold text-gray-900">Live Execution Traces</h2>
            <span className="text-xs font-medium px-2.5 py-0.5 rounded-full bg-blue-100 text-blue-800">Streaming</span>
          </div>
          <div className="flex-1 overflow-y-auto space-y-3 pr-2">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="p-3 bg-gray-50 rounded-lg border border-gray-100 flex items-start">
                <div className="mt-0.5 p-1.5 bg-indigo-100 text-indigo-600 rounded mr-3">
                  <Server size={16} />
                </div>
                <div className="flex-1">
                  <div className="flex justify-between items-center">
                    <p className="text-sm font-medium text-gray-900">SalesAgent_Execute</p>
                    <span className="text-xs text-gray-500">1.2s</span>
                  </div>
                  <p className="text-xs text-gray-500 mt-1 font-mono text-truncate">trace_id: a7x9b{i}2</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
