"use client";

import React from 'react';
import { Activity, Server, Cpu, AlertCircle, ShieldCheck } from 'lucide-react';
import StatCard from '@/components/dashboard/StatCard';
import TaskTrendChart from '@/components/dashboard/TaskTrendChart';
import RecentTasksTable from '@/components/dashboard/RecentTasksTable';
import AgentPieChart from '@/components/dashboard/AgentPieChart';
import SystemHealth from '@/components/dashboard/SystemHealth';
import HitlQueue from '@/components/dashboard/HitlQueue';

export default function CommandCenterPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-gray-800">Operations Control Center</h1>
          <p className="text-sm text-gray-500 mt-1">Real-time observability of your autonomous AI agents.</p>
        </div>
        <div className="flex space-x-2">
          <button className="bg-[#3c8dbc] hover:bg-[#367fa9] text-white px-4 py-2 rounded text-sm font-medium transition-colors shadow-sm flex items-center">
            <Activity size={16} className="mr-2" />
            Dispatch Task
          </button>
        </div>
      </div>

      {/* Top Stats Row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard 
          title="Active Temporal Workflows" 
          value="24" 
          icon={<Server size={48} className="opacity-80" />} 
          colorClass="bg-[#00c0ef]" 
        />
        <StatCard 
          title="LangGraph Success Rate" 
          value="99.2%" 
          icon={<ShieldCheck size={48} className="opacity-80" />} 
          colorClass="bg-[#00a65a]" 
        />
        <StatCard 
          title="HitL Approvals Pending" 
          value="3" 
          icon={<AlertCircle size={48} className="opacity-80" />} 
          colorClass="bg-[#f39c12]" 
        />
        <StatCard 
          title="Failed Agent Invocations" 
          value="1" 
          icon={<Cpu size={48} className="opacity-80" />} 
          colorClass="bg-[#dd4b39]" 
        />
      </div>

      {/* Middle Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <TaskTrendChart />
        </div>
        <div>
          <AgentPieChart />
        </div>
      </div>

      {/* Bottom Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <RecentTasksTable />
        </div>
        <div className="space-y-6">
          <SystemHealth />
          <HitlQueue />
        </div>
      </div>
    </div>
  );
}
