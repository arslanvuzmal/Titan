"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { Brain, Sparkles, TrendingUp, Cpu, Filter } from 'lucide-react';

export default function IntelligencePage() {
  return (
    <DashboardLayout>
      <div className="space-y-6 animate-fade-in">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-slate-200/80">
          <div>
            <h1 className="text-2xl font-bold text-slate-900 tracking-tight flex items-center">
              <Brain className="w-6 h-6 mr-2 text-blue-600" />
              Business Intelligence & AI Intent Matrix
            </h1>
            <p className="text-xs text-slate-500 mt-1">Autonomous decision models and target account scoring</p>
          </div>
          <button className="px-3.5 py-2 bg-white border border-slate-200 rounded-lg text-xs font-semibold text-slate-700 hover:bg-slate-50 transition-colors shadow-xs flex items-center">
            <Filter className="w-3.5 h-3.5 mr-1.5 text-slate-500" /> Filter Models
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
          <div className="bg-white p-5 rounded-xl border border-slate-200/80 shadow-xs space-y-2">
            <div className="flex justify-between items-center text-xs font-semibold text-slate-500 uppercase">
              <span>Avg Match Confidence</span>
              <Sparkles className="w-4 h-4 text-amber-500" />
            </div>
            <p className="text-3xl font-extrabold text-slate-900">94.8%</p>
            <p className="text-xs text-emerald-600 font-medium">+2.1% model accuracy this week</p>
          </div>

          <div className="bg-white p-5 rounded-xl border border-slate-200/80 shadow-xs space-y-2">
            <div className="flex justify-between items-center text-xs font-semibold text-slate-500 uppercase">
              <span>Intent Signals Processed</span>
              <TrendingUp className="w-4 h-4 text-blue-500" />
            </div>
            <p className="text-3xl font-extrabold text-slate-900">14,290</p>
            <p className="text-xs text-blue-600 font-medium">Across 850 tracked domains</p>
          </div>

          <div className="bg-white p-5 rounded-xl border border-slate-200/80 shadow-xs space-y-2">
            <div className="flex justify-between items-center text-xs font-semibold text-slate-500 uppercase">
              <span>Active LangChain Evaluators</span>
              <Cpu className="w-4 h-4 text-indigo-500" />
            </div>
            <p className="text-3xl font-extrabold text-slate-900">12</p>
            <p className="text-xs text-indigo-600 font-medium">Continuous vector embedding sync</p>
          </div>
        </div>

        <div className="bg-white p-6 rounded-xl border border-slate-200/80 shadow-xs space-y-4">
          <h3 className="font-bold text-slate-900 text-sm">High-Intent Account Recommendations</h3>
          <div className="space-y-3">
            {[
              { company: 'Acme Health Systems', score: '98%', reason: 'Hiring 5 AI Engineers & expanding cloud infrastructure', tier: 'Tier 1' },
              { company: 'Stripe Payments Inc', score: '95%', reason: 'High API query volume & developer platform expansion', tier: 'Tier 1' },
              { company: 'Vercel Platform Labs', score: '91%', reason: 'Next.js 16 deployment & edge runtime activity', tier: 'Tier 2' },
            ].map(item => (
              <div key={item.company} className="flex items-center justify-between p-4 rounded-lg bg-slate-50 border border-slate-200/60 hover:bg-slate-100/60 transition-colors">
                <div>
                  <h4 className="font-bold text-slate-900 text-sm">{item.company}</h4>
                  <p className="text-xs text-slate-500">{item.reason}</p>
                </div>
                <div className="text-right">
                  <span className="text-xs font-bold px-2.5 py-1 rounded bg-blue-100 text-blue-800 border border-blue-200">
                    {item.score} Intent
                  </span>
                  <p className="text-[10px] text-slate-400 font-mono mt-1">{item.tier}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </DashboardLayout>
  );
}
