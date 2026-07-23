"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { BarChart3, TrendingUp, DollarSign, Users } from 'lucide-react';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid } from 'recharts';

const analyticsData = [
  { day: 'Mon', conversion: 18, revenue: 14200 },
  { day: 'Tue', conversion: 24, revenue: 18900 },
  { day: 'Wed', conversion: 31, revenue: 24500 },
  { day: 'Thu', conversion: 28, revenue: 21000 },
  { day: 'Fri', conversion: 35, revenue: 29800 },
  { day: 'Sat', conversion: 20, revenue: 15400 },
  { day: 'Sun', conversion: 22, revenue: 17200 },
];

export default function AnalyticsPage() {
  return (
    <DashboardLayout>
      <div className="p-6 space-y-6 text-gray-800">
        <div className="flex items-center justify-between border-b pb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center">
              <BarChart3 className="w-6 h-6 mr-2 text-[#3c8dbc]" />
              Enterprise Pipeline Analytics
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Deep-dive metrics into lead conversion velocity, agent efficiency, and ROI.
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm flex items-center justify-between">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase">Weekly Velocity</p>
              <h3 className="text-2xl font-bold text-gray-900 mt-1">$141,000 ARR</h3>
            </div>
            <div className="p-3 bg-emerald-50 text-emerald-600 rounded-lg"><DollarSign className="w-6 h-6" /></div>
          </div>
          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm flex items-center justify-between">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase">Conversion Rate</p>
              <h3 className="text-2xl font-bold text-blue-600 mt-1">28.4%</h3>
            </div>
            <div className="p-3 bg-blue-50 text-blue-600 rounded-lg"><TrendingUp className="w-6 h-6" /></div>
          </div>
          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm flex items-center justify-between">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase">Total Qualified</p>
              <h3 className="text-2xl font-bold text-purple-600 mt-1">178 Deals</h3>
            </div>
            <div className="p-3 bg-purple-50 text-purple-600 rounded-lg"><Users className="w-6 h-6" /></div>
          </div>
        </div>

        <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm space-y-4">
          <h3 className="text-base font-bold text-gray-900">Weekly Performance Breakdown</h3>
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={analyticsData}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#eee" />
                <XAxis dataKey="day" tick={{ fontSize: 12 }} stroke="#888" />
                <YAxis yAxisId="left" tick={{ fontSize: 12 }} stroke="#888" />
                <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 12 }} stroke="#00a65a" />
                <Tooltip contentStyle={{ fontSize: 12 }} />
                <Line yAxisId="left" type="monotone" dataKey="revenue" stroke="#3c8dbc" strokeWidth={2.5} name="Revenue ($)" />
                <Line yAxisId="right" type="monotone" dataKey="conversion" stroke="#00a65a" strokeWidth={2} name="Conversions" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </DashboardLayout>
  );
}
