"use client";

import React from 'react';
import { Brain, Sparkles, TrendingUp, ShieldAlert, ArrowRight } from 'lucide-react';
import Link from 'next/link';

export function AIInsights() {
  return (
    <div className="bg-gradient-to-br from-slate-900 via-slate-800 to-indigo-950 text-white rounded-xl p-6 shadow-md border border-slate-700/60 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <div className="p-2 rounded-lg bg-blue-500/20 text-blue-400 border border-blue-500/30">
            <Brain className="h-5 w-5" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-white">Autonomous AI Insights</h3>
            <p className="text-[11px] text-slate-400">Predictive recommendations by TITAN Core</p>
          </div>
        </div>
        <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 flex items-center">
          <Sparkles className="w-3 h-3 mr-1" /> 98.4% Confidence
        </span>
      </div>

      <div className="space-y-3 pt-1">
        <div className="p-3.5 rounded-lg bg-slate-800/80 border border-slate-700/80 space-y-1 hover:border-slate-600 transition-colors">
          <div className="flex items-center justify-between text-xs">
            <span className="font-semibold text-blue-400 flex items-center">
              <TrendingUp className="w-3.5 h-3.5 mr-1" /> Outreach Velocity Spike
            </span>
            <span className="text-[10px] text-slate-400 font-mono">+34% Efficiency</span>
          </div>
          <p className="text-xs text-slate-300">
            SalesSDR agent identified 14 high-intent enterprise targets in SaaS sector with 92% match score.
          </p>
        </div>

        <div className="p-3.5 rounded-lg bg-slate-800/80 border border-slate-700/80 space-y-1 hover:border-slate-600 transition-colors">
          <div className="flex items-center justify-between text-xs">
            <span className="font-semibold text-amber-400 flex items-center">
              <ShieldAlert className="w-3.5 h-3.5 mr-1" /> Governance Review
            </span>
            <span className="text-[10px] text-slate-400 font-mono">1 Action Pending</span>
          </div>
          <p className="text-xs text-slate-300">
            FinanceBot flagged a $45,000 wire transfer exceeding standard threshold. Requires human authorization.
          </p>
        </div>
      </div>

      <div className="pt-2">
        <Link 
          href="/dashboard/intelligence" 
          className="w-full py-2.5 px-4 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-semibold text-xs flex items-center justify-center transition-colors shadow-sm"
        >
          View Full AI Intelligence Matrix <ArrowRight className="w-3.5 h-3.5 ml-1.5" />
        </Link>
      </div>
    </div>
  );
}
