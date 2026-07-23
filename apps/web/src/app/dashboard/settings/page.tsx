"use client";

import React, { useState } from 'react';
import DashboardLayout from '@/components/layout/DashboardLayout';
import { Settings, Save, Shield, Cpu } from 'lucide-react';

export default function SettingsPage() {
  const [model, setModel] = useState('gpt-4o');
  const [ssrfStrict, setSsrfStrict] = useState(true);
  const [temporalRetries, setTemporalRetries] = useState(5);

  return (
    <DashboardLayout>
      <div className="p-6 space-y-6 text-gray-800 max-w-4xl">
        <div className="flex items-center justify-between border-b pb-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center">
              <Settings className="w-6 h-6 mr-2 text-[#3c8dbc]" />
              Platform Settings & Configuration
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              Configure LLM backends, SSRF proxy boundaries, and Temporal execution policies.
            </p>
          </div>
          <button className="bg-[#3c8dbc] hover:bg-[#367fa9] text-white px-4 py-2 rounded text-sm font-semibold flex items-center shadow-sm">
            <Save className="w-4 h-4 mr-2" />
            Save Changes
          </button>
        </div>

        <div className="bg-white p-6 rounded-lg border border-gray-200 shadow-sm space-y-6">
          <div className="space-y-3">
            <h3 className="text-base font-bold text-gray-900 flex items-center">
              <Cpu className="w-4 h-4 mr-2 text-purple-600" />
              Primary LLM Provider
            </h3>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full p-2.5 border rounded-lg text-sm bg-white focus:outline-none focus:border-[#3c8dbc]"
            >
              <option value="gpt-4o">OpenAI GPT-4o (Default Recommended)</option>
              <option value="claude-3-5-sonnet">Anthropic Claude 3.5 Sonnet</option>
              <option value="gemini-1-5-pro">Google Gemini 1.5 Pro</option>
            </select>
          </div>

          <div className="pt-4 border-t space-y-3">
            <h3 className="text-base font-bold text-gray-900 flex items-center">
              <Shield className="w-4 h-4 mr-2 text-green-600" />
              Security & SSRF Policies
            </h3>
            <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg border">
              <div>
                <p className="text-sm font-medium text-gray-900">Enforce Strict SSRF Boundary</p>
                <p className="text-xs text-gray-500">Block all outbound agent HTTP requests to RFC 1918 private subnets.</p>
              </div>
              <input
                type="checkbox"
                checked={ssrfStrict}
                onChange={(e) => setSsrfStrict(e.target.checked)}
                className="w-5 h-5 accent-[#3c8dbc] cursor-pointer"
              />
            </div>
          </div>

          <div className="pt-4 border-t space-y-3">
            <h3 className="text-base font-bold text-gray-900">Temporal Workflow Retry Limit</h3>
            <input
              type="number"
              value={temporalRetries}
              onChange={(e) => setTemporalRetries(Number(e.target.value))}
              className="w-full p-2.5 border rounded-lg text-sm bg-white focus:outline-none focus:border-[#3c8dbc]"
            />
          </div>
        </div>
      </div>
    </DashboardLayout>
  );
}
