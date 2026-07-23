"use client";

import React from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { Brain, TrendingUp, Sparkles, Target, Zap } from 'lucide-react';
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid } from 'recharts';

const intelData = [
  { metric: 'Lead Intent', score: 94 },
  { metric: 'Tech Fit', score: 88 },
  { metric: 'Budget Authority', score: 76 },
  { metric: 'Engagement', score: 92 },
  { metric: 'Conversion Odds', score: 85 },
];

export default function IntelligencePage() {
  return (
    <DashboardLayout>
      <div className="p-6 space-y-6 text-gray-800">
        <div className="flex items-center justify-between border-b pb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center">
              <Brain className="w-6 h-6 mr-2 text-[#3c8dbc]" />
              Business Intelligence & AI Insights
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Automated competitive analysis, market intent scoring, and predictive revenue models.
            </p>
          </div>
          <span className="bg-purple-100 text-purple-800 text-xs font-semibold px-3 py-1 rounded-full border border-purple-200">
            GPT-4o Engine Active
          </span>
        </div>

        {/* Top Insights Cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm space-y-2">
            <div className="flex items-center space-x-2 text-blue-600 font-bold text-sm">
              <Sparkles className="w-4 h-4" />
              <span>High-Value Recommendation</span>
            </div>
            <h3 className="font-bold text-gray-900 text-base">Upsell Acme Corp to Enterprise Plan</h3>
            <p className="text-xs text-gray-500 leading-relaxed">
              Usage patterns indicate 450+ API requests/min. Predicted ARR increase: +$85,000.
            </p>
          </div>

          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm space-y-2">
            <div className="flex items-center space-x-2 text-emerald-600 font-bold text-sm">
              <TrendingUp className="w-4 h-4" />
              <span>Market Trend Alert</span>
            </div>
            <h3 className="font-bold text-gray-900 text-base">Fintech Demand Surge +34%</h3>
            <p className="text-xs text-gray-500 leading-relaxed">
              Inbound lead volume from Fintech verticals doubled over the last 14 days.
            </p>
          </div>

          <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm space-y-2">
            <div className="flex items-center space-x-2 text-indigo-600 font-bold text-sm">
              <Target className="w-4 h-4" />
              <span>Competitor Displacement</span>
            </div>
            <h3 className="font-bold text-gray-900 text-base">3 Accounts Considering Migration</h3>
            <p className="text-xs text-gray-500 leading-relaxed">
              ResearchBot identified RFP activity matching TITAN features at Global Retail Inc.
            </p>
          </div>
        </div>

        {/* Intelligence Chart */}
        <div className="bg-white p-5 rounded-lg border border-gray-200 shadow-sm space-y-4">
          <h3 className="text-base font-bold text-gray-900 flex items-center">
            <Zap className="w-4 h-4 mr-2 text-amber-500" />
            AI Predictive Intent Matrix
          </h3>
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={intelData}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#eee" />
                <XAxis dataKey="metric" tick={{ fontSize: 12 }} stroke="#888" />
                <YAxis tick={{ fontSize: 12 }} stroke="#888" domain={[0, 100]} />
                <Tooltip contentStyle={{ fontSize: 12 }} />
                <Bar dataKey="score" fill="#3c8dbc" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </DashboardLayout>
  );
}
