"use client";

import React, { useState, useEffect } from 'react';
import { 
  DollarSign, Users, AlertTriangle, Cpu, CheckCircle2, Smile, 
  RefreshCw, Play, Sparkles, TrendingUp, 
  ShieldAlert, Target, ArrowUpRight
} from 'lucide-react';
import ProductTour from '@/components/onboarding/ProductTour';
import { InfoTooltip, FeatureShowcaseModal } from '@/components/help/ContextualHelp';
import { INITIAL_REALTIME_ACTIVITIES, getSeededData } from '@/lib/demoMode';
import { 
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, 
  PieChart, Pie, Cell, BarChart, Bar, CartesianGrid 
} from 'recharts';

export default function ExpandedDashboardPage() {
  const seeded = getSeededData();
  const [tourOpen, setTourOpen] = useState(false);
  const [demoMode, setDemoMode] = useState(false);
  const [modalTopic, setModalTopic] = useState<'SCORING' | 'HITL' | 'RAG' | 'TEMPORAL' | null>(null);
  const [activities, setActivities] = useState(INITIAL_REALTIME_ACTIVITIES);
  const [lastRefreshed, setLastRefreshed] = useState("Just now");

  // Simulated real-time incoming activity when Demo Mode is active
  useEffect(() => {
    if (!demoMode) return;
    const interval = setInterval(() => {
      const newActivity = {
        id: `act-live-${Date.now()}`,
        type: 'LEAD' as const,
        title: `Real-Time Event #${Math.floor(Math.random() * 899 + 100)}`,
        subtitle: `Scored Lead from Inbound Webhook (${Math.floor(Math.random() * 20 + 80)}/100)`,
        status: "QUALIFIED",
        timestamp: "Just now",
        badgeColor: "bg-[#00a65a] text-white"
      };
      setActivities(prev => [newActivity, ...prev.slice(0, 6)]);
    }, 6000);
    return () => clearInterval(interval);
  }, [demoMode]);

  const handleRefresh = () => {
    setLastRefreshed(new Date().toLocaleTimeString());
  };

  // Chart dataset for 30 days
  const lineChartData = seeded.metrics_history.slice(-30).map(m => ({
    date: m.date.slice(5),
    revenue: m.revenue,
    leads: m.leads_converted
  }));

  const pieData = [
    { name: 'Organic Search', value: 42, color: '#3c8dbc' },
    { name: 'Paid Campaign', value: 28, color: '#00a65a' },
    { name: 'Referral', value: 18, color: '#f39c12' },
    { name: 'Direct Outbound', value: 12, color: '#dd4b39' },
  ];

  const barData = [
    { agent: 'SalesSDR', tasks: 340 },
    { agent: 'ResearchBot', tasks: 280 },
    { agent: 'SupportAgent', tasks: 210 },
    { agent: 'BIEngineer', tasks: 160 },
    { agent: 'RiskClassifier', tasks: 190 },
  ];

  return (
    <div className="w-full min-h-screen bg-[#f4f6f9] p-4 md:p-6 space-y-6 text-gray-800">
      
      {/* Top Header & Interactive Demo Controls */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-white p-4 rounded-lg shadow-sm border border-gray-200">
        <div>
          <div className="flex items-center space-x-3">
            <h1 className="text-2xl font-bold text-gray-900">TITAN Operations Command Center</h1>
            <span className="bg-blue-100 text-blue-800 text-xs font-semibold px-2.5 py-0.5 rounded border border-blue-200">
              Enterprise v2.4
            </span>
          </div>
          <p className="text-sm text-gray-500 mt-1">
            Real-time multi-agent execution telemetry • Last refreshed: {lastRefreshed}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Demo Mode Toggle */}
          <button
            onClick={() => setDemoMode(!demoMode)}
            className={`px-3 py-1.5 rounded text-xs font-semibold flex items-center transition-all shadow-sm ${
              demoMode 
                ? 'bg-emerald-600 hover:bg-emerald-700 text-white animate-pulse' 
                : 'bg-gray-100 hover:bg-gray-200 text-gray-700 border border-gray-300'
            }`}
          >
            <Play className="w-3.5 h-3.5 mr-1.5" />
            {demoMode ? "Live Demo Active (30s Feed)" : "Enable Live Demo Mode"}
          </button>

          {/* Product Tour Button */}
          <button
            onClick={() => setTourOpen(true)}
            className="bg-[#3c8dbc] hover:bg-[#367fa9] text-white px-3 py-1.5 rounded text-xs font-semibold transition-colors shadow-sm flex items-center"
          >
            <Sparkles className="w-3.5 h-3.5 mr-1.5" />
            Take Guided Tour
          </button>

          {/* Refresh Data */}
          <button
            onClick={handleRefresh}
            className="p-1.5 bg-gray-100 hover:bg-gray-200 text-gray-600 rounded border border-gray-300 transition-colors"
            title="Refresh All Data"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* SECTION A: EXECUTIVE SUMMARY (6 Stat Cards) */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4">
        {/* Card 1 */}
        <div className="bg-white p-4 rounded-lg shadow-sm border border-gray-200 relative overflow-hidden">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Total Revenue</p>
              <h3 className="text-2xl font-bold text-gray-900 mt-1">$1,482,900</h3>
            </div>
            <div className="p-2 bg-emerald-50 rounded-lg text-emerald-600"><DollarSign className="w-5 h-5" /></div>
          </div>
          <div className="mt-3 flex items-center text-xs text-emerald-600 font-semibold">
            <TrendingUp className="w-3.5 h-3.5 mr-1" /> +14.2% MoM
          </div>
        </div>

        {/* Card 2 */}
        <div className="bg-white p-4 rounded-lg shadow-sm border border-gray-200 relative overflow-hidden">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Active Leads</p>
              <h3 className="text-2xl font-bold text-gray-900 mt-1">54 Prospects</h3>
            </div>
            <div className="p-2 bg-blue-50 rounded-lg text-blue-600"><Users className="w-5 h-5" /></div>
          </div>
          <div className="mt-3 text-xs text-gray-500 font-medium">
            Avg Score: <span className="font-semibold text-blue-600">84/100</span>
          </div>
        </div>

        {/* Card 3 */}
        <div className="bg-white p-4 rounded-lg shadow-sm border border-gray-200 relative overflow-hidden">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Pending Approvals</p>
              <h3 className="text-2xl font-bold text-amber-600 mt-1">3 Urgent</h3>
            </div>
            <div className="p-2 bg-amber-50 rounded-lg text-amber-600"><AlertTriangle className="w-5 h-5" /></div>
          </div>
          <button 
            onClick={() => setModalTopic('HITL')}
            className="mt-3 text-xs text-amber-600 hover:underline font-medium flex items-center"
          >
            Review HITL Queue <ArrowUpRight className="w-3 h-3 ml-0.5" />
          </button>
        </div>

        {/* Card 4 */}
        <div className="bg-white p-4 rounded-lg shadow-sm border border-gray-200 relative overflow-hidden">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">AI Agents Active</p>
              <h3 className="text-2xl font-bold text-purple-600 mt-1">6 Running</h3>
            </div>
            <div className="p-2 bg-purple-50 rounded-lg text-purple-600"><Cpu className="w-5 h-5" /></div>
          </div>
          <div className="mt-3 text-xs text-purple-600 font-medium">
            LangGraph Multi-Agent
          </div>
        </div>

        {/* Card 5 */}
        <div className="bg-white p-4 rounded-lg shadow-sm border border-gray-200 relative overflow-hidden">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Tasks Today</p>
              <h3 className="text-2xl font-bold text-gray-900 mt-1">104 Tasks</h3>
            </div>
            <div className="p-2 bg-green-50 rounded-lg text-green-600"><CheckCircle2 className="w-5 h-5" /></div>
          </div>
          <div className="mt-3 text-xs text-green-600 font-medium">
            99.2% Success Rate
          </div>
        </div>

        {/* Card 6 */}
        <div className="bg-white p-4 rounded-lg shadow-sm border border-gray-200 relative overflow-hidden">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">CSAT Score</p>
              <h3 className="text-2xl font-bold text-gray-900 mt-1">4.92 / 5.0</h3>
            </div>
            <div className="p-2 bg-indigo-50 rounded-lg text-indigo-600"><Smile className="w-5 h-5" /></div>
          </div>
          <div className="mt-3 text-xs text-gray-500 font-medium">
            90-Day Enterprise Avg
          </div>
        </div>
      </div>

      {/* SECTION B: VISUAL ANALYTICS */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Large Line Chart */}
        <div className="lg:col-span-2 bg-white p-5 rounded-lg shadow-sm border border-gray-200 space-y-4">
          <div className="flex justify-between items-center">
            <div>
              <h3 className="text-base font-bold text-gray-900 flex items-center">
                Revenue & Lead Conversion Trends (Last 30 Days)
                <InfoTooltip text="Aggregated daily metrics generated from temporal workflow execution logs." />
              </h3>
              <p className="text-xs text-gray-500">Track financial velocity against autonomous lead conversions.</p>
            </div>
          </div>
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={lineChartData}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#eee" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} stroke="#888" />
                <YAxis yAxisId="left" tick={{ fontSize: 11 }} stroke="#888" />
                <YAxis yAxisId="right" orientation="right" tick={{ fontSize: 11 }} stroke="#00a65a" />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6 }} />
                <Line yAxisId="left" type="monotone" dataKey="revenue" stroke="#3c8dbc" strokeWidth={2.5} dot={false} name="Revenue ($)" />
                <Line yAxisId="right" type="monotone" dataKey="leads" stroke="#00a65a" strokeWidth={2} dot={false} name="Leads Converted" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Lead Sources Pie Chart */}
        <div className="bg-white p-5 rounded-lg shadow-sm border border-gray-200 space-y-4">
          <h3 className="text-base font-bold text-gray-900 flex items-center">
            Lead Source Attribution
            <InfoTooltip text="Channel breakdown for 54 active enterprise leads in current pipeline." />
          </h3>
          <div className="h-48 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={pieData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={70} label>
                  {pieData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip contentStyle={{ fontSize: 11 }} />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="grid grid-cols-2 gap-2 text-xs">
            {pieData.map((p, idx) => (
              <div key={idx} className="flex items-center space-x-1.5">
                <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: p.color }} />
                <span className="text-gray-600 truncate">{p.name}: <b>{p.value}%</b></span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* SECTION C: AI INTELLIGENCE PANEL */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Sales Insights */}
        <div className="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-lg shadow-md relative overflow-hidden">
          <div className="flex items-center space-x-2 text-blue-300 font-bold text-sm mb-2">
            <Sparkles className="w-4 h-4 text-amber-400" />
            <span>AI Sales Recommendations</span>
          </div>
          <h4 className="text-base font-bold mb-2">Target Acme Corp & Client Enterprise 14</h4>
          <p className="text-xs text-blue-100 leading-relaxed mb-4">
            ResearchBot detected tech stack migration at Acme Corp. Outreach response probability is estimated at 89%.
          </p>
          <button 
            onClick={() => setModalTopic('SCORING')}
            className="text-xs bg-blue-500/30 hover:bg-blue-500/50 border border-blue-400/40 text-white px-3 py-1.5 rounded transition-colors font-medium"
          >
            How Lead Scoring Works →
          </button>
        </div>

        {/* Risk Alerts */}
        <div className="bg-gradient-to-br from-amber-900 to-orange-950 text-white p-5 rounded-lg shadow-md relative overflow-hidden">
          <div className="flex items-center space-x-2 text-amber-300 font-bold text-sm mb-2">
            <ShieldAlert className="w-4 h-4 text-amber-400" />
            <span>Security Anomaly Detection</span>
          </div>
          <h4 className="text-base font-bold mb-2">SSRF Proxy Intercepted 2 Calls</h4>
          <p className="text-xs text-amber-100 leading-relaxed mb-4">
            Custom scraper attempted to query internal AWS metadata IP (169.254.169.254). Request was safely blocked.
          </p>
          <button 
            onClick={() => setModalTopic('TEMPORAL')}
            className="text-xs bg-amber-500/30 hover:bg-amber-500/50 border border-amber-400/40 text-white px-3 py-1.5 rounded transition-colors font-medium"
          >
            Inspect Security Audit →
          </button>
        </div>

        {/* Opportunities */}
        <div className="bg-gradient-to-br from-emerald-900 to-teal-950 text-white p-5 rounded-lg shadow-md relative overflow-hidden">
          <div className="flex items-center space-x-2 text-emerald-300 font-bold text-sm mb-2">
            <Target className="w-4 h-4 text-emerald-400" />
            <span>High-Value Expansion</span>
          </div>
          <h4 className="text-base font-bold mb-2">$320,000 Potential Pipeline</h4>
          <p className="text-xs text-emerald-100 leading-relaxed mb-4">
            3 enterprise accounts qualify for automatic expansion sequences based on RAG knowledge queries.
          </p>
          <button 
            onClick={() => setModalTopic('RAG')}
            className="text-xs bg-emerald-500/30 hover:bg-emerald-500/50 border border-emerald-400/40 text-white px-3 py-1.5 rounded transition-colors font-medium"
          >
            View RAG Context →
          </button>
        </div>
      </div>

      {/* SECTION D: REAL-TIME ACTIVITY & RECENT APPROVALS */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Real-time Activity Feed */}
        <div className="lg:col-span-2 bg-white p-5 rounded-lg shadow-sm border border-gray-200 space-y-4">
          <div className="flex justify-between items-center border-b pb-3">
            <h3 className="text-base font-bold text-gray-900">Live Multi-Agent Task Stream</h3>
            <span className="text-xs font-semibold text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
              WebSocket Connected
            </span>
          </div>
          <div className="space-y-3 max-h-80 overflow-y-auto pr-1">
            {activities.map((act) => (
              <div key={act.id} className="p-3 bg-gray-50 hover:bg-blue-50/40 rounded border border-gray-200/80 flex items-center justify-between transition-colors">
                <div>
                  <div className="flex items-center space-x-2">
                    <span className="text-xs font-bold text-gray-900">{act.title}</span>
                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded border ${act.badgeColor}`}>
                      {act.status}
                    </span>
                  </div>
                  <p className="text-xs text-gray-500 mt-0.5">{act.subtitle}</p>
                </div>
                <span className="text-[11px] text-gray-400 font-mono">{act.timestamp}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Agent Volume Bar Chart */}
        <div className="bg-white p-5 rounded-lg shadow-sm border border-gray-200 space-y-4">
          <h3 className="text-base font-bold text-gray-900">Task Volume per Agent</h3>
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={barData}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#eee" />
                <XAxis dataKey="agent" tick={{ fontSize: 10 }} stroke="#888" />
                <YAxis tick={{ fontSize: 11 }} stroke="#888" />
                <Tooltip contentStyle={{ fontSize: 11 }} />
                <Bar dataKey="tasks" fill="#3c8dbc" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>

      {/* Product Tour Overlay */}
      <ProductTour isOpen={tourOpen} onClose={() => setTourOpen(false)} />

      {/* Feature Showcase Modal */}
      {modalTopic && (
        <FeatureShowcaseModal 
          topic={modalTopic} 
          isOpen={!!modalTopic} 
          onClose={() => setModalTopic(null)} 
        />
      )}
    </div>
  );
}
