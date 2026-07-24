"use client";

import React, { useState } from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { Settings, Save, Shield, Cpu, Key } from 'lucide-react';

export default function SettingsPage() {
  const [model, setModel] = useState('gpt-4o');
  const [ssrfGuard, setSsrfGuard] = useState(true);
  const [autoApproveLowRisk, setAutoApproveLowRisk] = useState(false);

  return (
    <DashboardLayout>
      <div className="space-y-6 max-w-4xl animate-fade-in">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-slate-200/80">
          <div>
            <h1 className="text-2xl font-bold text-slate-900 tracking-tight flex items-center">
              <Settings className="w-6 h-6 mr-2 text-blue-600" />
              Platform Settings & Engine Configuration
            </h1>
            <p className="text-xs text-slate-500 mt-1">Configure LLM providers, SSRF security proxies, and HITL approval thresholds</p>
          </div>
          <button className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-xs font-semibold shadow-sm transition-colors flex items-center">
            <Save className="w-3.5 h-3.5 mr-1.5" /> Save Changes
          </button>
        </div>

        {/* LLM Model Provider */}
        <div className="bg-white p-6 rounded-xl border border-slate-200/80 shadow-xs space-y-4">
          <h3 className="font-bold text-slate-900 text-sm flex items-center">
            <Cpu className="w-4 h-4 mr-2 text-blue-600" /> Primary Reasoning LLM Engine
          </h3>
          <div className="space-y-2">
            <label className="text-xs font-medium text-slate-700">Select Model Architecture</label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full p-2.5 bg-slate-50 border border-slate-200 rounded-lg text-xs text-slate-900 focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500"
            >
              <option value="gpt-4o">OpenAI GPT-4o (Default Recommended)</option>
              <option value="claude-3-5-sonnet">Anthropic Claude 3.5 Sonnet</option>
              <option value="gemini-1-5-pro">Google Gemini 1.5 Pro</option>
            </select>
          </div>
        </div>

        {/* Security Settings */}
        <div className="bg-white p-6 rounded-xl border border-slate-200/80 shadow-xs space-y-4">
          <h3 className="font-bold text-slate-900 text-sm flex items-center">
            <Shield className="w-4 h-4 mr-2 text-emerald-600" /> Egress Proxy & SSRF Defense Rules
          </h3>
          <div className="flex items-center justify-between p-3 rounded-lg bg-slate-50 border border-slate-200/60">
            <div>
              <p className="text-xs font-bold text-slate-900">Enforce Zero-Trust SSRF Validation</p>
              <p className="text-[11px] text-slate-500">Block requests targeting localhost, 127.0.0.1, or AWS metadata IPs</p>
            </div>
            <input
              type="checkbox"
              checked={ssrfGuard}
              onChange={(e) => setSsrfGuard(e.target.checked)}
              className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
            />
          </div>

          <div className="flex items-center justify-between p-3 rounded-lg bg-slate-50 border border-slate-200/60">
            <div>
              <p className="text-xs font-bold text-slate-900">Auto-Approve Low Risk Actions (&lt; $500)</p>
              <p className="text-[11px] text-slate-500">Bypass HITL manual verification for low impact actions</p>
            </div>
            <input
              type="checkbox"
              checked={autoApproveLowRisk}
              onChange={(e) => setAutoApproveLowRisk(e.target.checked)}
              className="w-4 h-4 text-blue-600 rounded focus:ring-blue-500"
            />
          </div>
        </div>

        {/* API Credentials */}
        <div className="bg-white p-6 rounded-xl border border-slate-200/80 shadow-xs space-y-4">
          <h3 className="font-bold text-slate-900 text-sm flex items-center">
            <Key className="w-4 h-4 mr-2 text-indigo-600" /> API Keys & Provider Credentials
          </h3>
          <div className="space-y-3">
            <div>
              <label className="text-xs font-medium text-slate-700">OpenAI API Key</label>
              <input
                type="password"
                value="sk-proj-********************************"
                disabled
                className="w-full mt-1 p-2.5 bg-slate-100 border border-slate-200 rounded-lg text-xs text-slate-600 font-mono"
              />
            </div>
          </div>
        </div>
      </div>
    </DashboardLayout>
  );
}
