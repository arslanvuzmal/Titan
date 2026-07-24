"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import StatCard from '@/components/dashboard/StatCard';
import TaskTrendChart from '@/components/dashboard/TaskTrendChart';
import RecentTasksTable from '@/components/dashboard/RecentTasksTable';
import { AIInsights } from '@/components/dashboard/AIInsights';
import { Activity, Clock, CheckCircle2, Cpu, ArrowUpRight, Sparkles, Filter, Download } from 'lucide-react';

export default function DashboardPage() {
  return (
    <DashboardLayout>
      <div className="space-y-8 animate-fade-in">
        {/* Page Header */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-slate-200/80">
          <div>
            <div className="flex items-center space-x-2">
              <h1 className="text-2xl md:text-3xl font-extrabold text-slate-900 tracking-tight">Command Center</h1>
              <span className="bg-blue-100 text-blue-800 text-[11px] font-bold px-2.5 py-0.5 rounded-full border border-blue-200 flex items-center">
                <Sparkles className="w-3 h-3 mr-1 text-blue-600" /> v2.4 Live
              </span>
            </div>
            <p className="text-sm text-slate-500 mt-1">Real-time multi-agent execution telemetry and governance platform</p>
          </div>

          <div className="flex items-center space-x-3">
            <button className="px-3.5 py-2 bg-white border border-slate-200 rounded-lg text-xs font-semibold text-slate-700 hover:bg-slate-50 transition-colors shadow-xs flex items-center">
              <Filter className="w-3.5 h-3.5 mr-1.5 text-slate-500" /> Filter
            </button>
            <button className="px-3.5 py-2 bg-white border border-slate-200 rounded-lg text-xs font-semibold text-slate-700 hover:bg-slate-50 transition-colors shadow-xs flex items-center">
              <Download className="w-3.5 h-3.5 mr-1.5 text-slate-500" /> Export Logs
            </button>
            <button className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors flex items-center">
              Dispatch Workflow <ArrowUpRight className="w-3.5 h-3.5 ml-1.5" />
            </button>
          </div>
        </div>

        {/* Stats Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
          <StatCard
            title="Active Workflow Executions"
            value="24"
            change="+14.2%"
            trend="up"
            icon={Activity}
            iconColor="text-blue-600"
            iconBg="bg-blue-50 border-blue-100"
          />
          <StatCard
            title="Pending HITL Approvals"
            value="7"
            change="-2.5%"
            trend="down"
            icon={Clock}
            iconColor="text-amber-600"
            iconBg="bg-amber-50 border-amber-100"
          />
          <StatCard
            title="Agent Execution Success"
            value="98.4%"
            change="+1.1%"
            trend="up"
            icon={CheckCircle2}
            iconColor="text-emerald-600"
            iconBg="bg-emerald-50 border-emerald-100"
          />
          <StatCard
            title="Active LangGraph Nodes"
            value="5"
            change="Stable"
            trend="neutral"
            icon={Cpu}
            iconColor="text-indigo-600"
            iconBg="bg-indigo-50 border-indigo-100"
          />
        </div>

        {/* Charts & AI Insights Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2">
            <TaskTrendChart />
          </div>
          <div>
            <AIInsights />
          </div>
        </div>

        {/* Recent Tasks Table */}
        <RecentTasksTable />
      </div>
    </DashboardLayout>
  );
}
