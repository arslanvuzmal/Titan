"use client";

import React from 'react';
import { PlayCircle, Clock, CheckCircle, AlertTriangle } from 'lucide-react';
import StatCard from '@/components/dashboard/StatCard';
import TaskTrendChart from '@/components/dashboard/TaskTrendChart';
import RecentTasksTable from '@/components/dashboard/RecentTasksTable';

export default function CommandCenterPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-gray-800">Command Center</h1>
        <div className="flex space-x-2">
          <button className="bg-[#3c8dbc] hover:bg-[#367fa9] text-white px-4 py-2 rounded text-sm transition-colors shadow-sm">
            + New Task
          </button>
        </div>
      </div>

      {/* Top Stats Row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard 
          title="Active Tasks" 
          value="12" 
          icon={<PlayCircle size={48} />} 
          colorClass="bg-[#00c0ef]" 
        />
        <StatCard 
          title="Success Rate" 
          value="98.5%" 
          icon={<CheckCircle size={48} />} 
          colorClass="bg-[#00a65a]" 
        />
        <StatCard 
          title="Pending Approvals" 
          value="4" 
          icon={<Clock size={48} />} 
          colorClass="bg-[#f39c12]" 
        />
        <StatCard 
          title="Failed Operations" 
          value="1" 
          icon={<AlertTriangle size={48} />} 
          colorClass="bg-[#dd4b39]" 
        />
      </div>

      {/* Middle Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <TaskTrendChart />
        </div>
        <div className="bg-white rounded shadow-sm p-4 flex flex-col items-center justify-center min-h-[300px]">
          <h3 className="text-lg font-medium text-gray-800 mb-4 self-start">Agent Distribution</h3>
          {/* Placeholder for Pie Chart */}
          <div className="w-48 h-48 rounded-full border-[16px] border-[#3c8dbc] border-r-[#00a65a] border-b-[#f39c12] border-l-[#dd4b39] flex items-center justify-center">
            <span className="text-gray-500 font-medium">Distribution</span>
          </div>
          <div className="flex flex-wrap justify-center gap-3 mt-6 text-xs text-gray-600">
            <span className="flex items-center"><span className="w-3 h-3 rounded-full bg-[#3c8dbc] mr-1"></span> Finance</span>
            <span className="flex items-center"><span className="w-3 h-3 rounded-full bg-[#00a65a] mr-1"></span> Sales</span>
            <span className="flex items-center"><span className="w-3 h-3 rounded-full bg-[#f39c12] mr-1"></span> HR</span>
            <span className="flex items-center"><span className="w-3 h-3 rounded-full bg-[#dd4b39] mr-1"></span> Intel</span>
          </div>
        </div>
      </div>

      {/* Bottom Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <RecentTasksTable />
        </div>
        <div className="bg-white rounded shadow-sm">
          <div className="px-4 py-3 border-b border-gray-100">
            <h3 className="font-medium text-gray-800">Activity Timeline</h3>
          </div>
          <div className="p-4">
            <ul className="space-y-4">
              <li className="flex">
                <div className="flex-shrink-0 w-8 flex flex-col items-center">
                  <div className="h-3 w-3 rounded-full bg-[#00a65a]"></div>
                  <div className="h-full w-px bg-gray-200 mt-1"></div>
                </div>
                <div className="pb-4">
                  <p className="text-sm font-medium text-gray-800">FinanceBot completed Q3 Report</p>
                  <p className="text-xs text-gray-500 mt-0.5">10 mins ago</p>
                </div>
              </li>
              <li className="flex">
                <div className="flex-shrink-0 w-8 flex flex-col items-center">
                  <div className="h-3 w-3 rounded-full bg-[#f39c12]"></div>
                  <div className="h-full w-px bg-gray-200 mt-1"></div>
                </div>
                <div className="pb-4">
                  <p className="text-sm font-medium text-gray-800">HR-Assistant requested approval</p>
                  <p className="text-xs text-gray-500 mt-0.5">1 hour ago</p>
                </div>
              </li>
              <li className="flex">
                <div className="flex-shrink-0 w-8 flex flex-col items-center">
                  <div className="h-3 w-3 rounded-full bg-[#dd4b39]"></div>
                </div>
                <div>
                  <p className="text-sm font-medium text-gray-800">MarketIntel failed to scrape data</p>
                  <p className="text-xs text-gray-500 mt-0.5">2 hours ago</p>
                </div>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
