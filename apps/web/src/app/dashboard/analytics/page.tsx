"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { BarChart3, TrendingUp, DollarSign, Zap } from 'lucide-react';

export default function AnalyticsPage() {
  return (
    <DashboardLayout>
      <div className="space-y-6 animate-fade-in">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-slate-200/80">
          <div>
            <h1 className="text-2xl font-bold text-slate-900 tracking-tight flex items-center">
              <BarChart3 className="w-6 h-6 mr-2 text-blue-600" />
              Pipeline Velocity & Agent ROI Analytics
            </h1>
            <p className="text-xs text-slate-500 mt-1">Quantify business impact, pipeline conversions, and token efficiency</p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
          <div className="bg-white p-5 rounded-xl border border-slate-200/80 shadow-xs space-y-2">
            <div className="flex justify-between items-center text-xs font-semibold text-slate-500 uppercase">
              <span>Pipeline Value Generated</span>
              <DollarSign className="w-4 h-4 text-emerald-600" />
            </div>
            <p className="text-3xl font-extrabold text-slate-900">$1,450,000</p>
            <p className="text-xs text-emerald-600 font-medium">+18.4% vs last month</p>
          </div>

          <div className="bg-white p-5 rounded-xl border border-slate-200/80 shadow-xs space-y-2">
            <div className="flex justify-between items-center text-xs font-semibold text-slate-500 uppercase">
              <span>Hours Saved via AI Agents</span>
              <Zap className="w-4 h-4 text-amber-500" />
            </div>
            <p className="text-3xl font-extrabold text-slate-900">1,240 hrs</p>
            <p className="text-xs text-blue-600 font-medium">Equivalent to 7.5 FTEs</p>
          </div>

          <div className="bg-white p-5 rounded-xl border border-slate-200/80 shadow-xs space-y-2">
            <div className="flex justify-between items-center text-xs font-semibold text-slate-500 uppercase">
              <span>Token Cost Efficiency</span>
              <TrendingUp className="w-4 h-4 text-blue-500" />
            </div>
            <p className="text-3xl font-extrabold text-slate-900">$0.0042</p>
            <p className="text-xs text-emerald-600 font-medium">Per qualified lead generated</p>
          </div>
        </div>
      </div>
    </DashboardLayout>
  );
}
