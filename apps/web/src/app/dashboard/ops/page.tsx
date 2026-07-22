"use client";

import React, { useState } from 'react';
import { Activity, Cpu, CheckCircle, Clock, Zap, X, ChevronRight } from 'lucide-react';

interface Trace {
  id: string;
  name: string;
  status: 'success' | 'failed';
  latency: string;
  tokens: number;
  cost: string;
  time: string;
}

export default function AIOpsDashboard() {
  const [selectedTrace, setSelectedTrace] = useState<Trace | null>(null);

  // Mock data for the live traces table
  const mockTraces: Trace[] = [
    { id: "tr_1abc", name: "SalesPipelineWorkflow", status: "success", latency: "1.2s", tokens: 450, cost: "$0.004", time: "Just now" },
    { id: "tr_2xyz", name: "Generate Outreach Email", status: "success", latency: "3.4s", tokens: 1200, cost: "$0.012", time: "2m ago" },
    { id: "tr_3def", name: "HubSpot: Update Deal", status: "success", latency: "0.4s", tokens: 0, cost: "$0.000", time: "5m ago" },
    { id: "tr_4ghi", name: "Lead Validation", status: "failed", latency: "0.1s", tokens: 0, cost: "$0.000", time: "12m ago" },
  ];

  return (
    <div className="max-w-7xl mx-auto py-8">
      <div className="flex justify-between items-center mb-8">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">AI Operations</h1>
          <p className="text-gray-500 mt-1">Real-time observability and token economics.</p>
        </div>
        <div className="flex items-center space-x-2 text-sm text-green-600 bg-green-50 px-3 py-1 rounded-full font-medium">
          <Activity className="w-4 h-4 animate-pulse" />
          <span>OTLP Telemetry Active</span>
        </div>
      </div>

      {/* Top Section: Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200">
          <div className="flex items-center justify-between">
            <h3 className="text-gray-500 text-sm font-medium">Total Tokens (24h)</h3>
            <Cpu className="w-5 h-5 text-indigo-500" />
          </div>
          <p className="text-3xl font-bold text-gray-900 mt-2">142.5k</p>
          <p className="text-xs text-green-600 mt-2">↓ 12% vs yesterday</p>
        </div>
        
        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200">
          <div className="flex items-center justify-between">
            <h3 className="text-gray-500 text-sm font-medium">Avg Agent Latency</h3>
            <Clock className="w-5 h-5 text-blue-500" />
          </div>
          <p className="text-3xl font-bold text-gray-900 mt-2">2.4s</p>
          <p className="text-xs text-red-600 mt-2">↑ 0.2s vs yesterday</p>
        </div>

        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200">
          <div className="flex items-center justify-between">
            <h3 className="text-gray-500 text-sm font-medium">Tool Success Rate</h3>
            <CheckCircle className="w-5 h-5 text-green-500" />
          </div>
          <p className="text-3xl font-bold text-gray-900 mt-2">99.8%</p>
          <p className="text-xs text-gray-500 mt-2">2,410 executions</p>
        </div>

        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200">
          <div className="flex items-center justify-between">
            <h3 className="text-gray-500 text-sm font-medium">Est. Compute Cost</h3>
            <Zap className="w-5 h-5 text-yellow-500" />
          </div>
          <p className="text-3xl font-bold text-gray-900 mt-2">$14.20</p>
          <p className="text-xs text-gray-500 mt-2">Current billing cycle</p>
        </div>
      </div>

      {/* Middle Section: Live Traces */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200 overflow-hidden mb-8">
        <div className="px-6 py-4 border-b border-gray-200 bg-gray-50 flex justify-between items-center">
          <h3 className="font-semibold text-gray-800">Live Execution Traces</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm text-left">
            <thead className="text-xs text-gray-500 uppercase bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-6 py-3">Trace ID</th>
                <th className="px-6 py-3">Span Name</th>
                <th className="px-6 py-3">Status</th>
                <th className="px-6 py-3">Latency</th>
                <th className="px-6 py-3">Tokens</th>
                <th className="px-6 py-3">Cost</th>
                <th className="px-6 py-3">Time</th>
                <th className="px-6 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {mockTraces.map((trace) => (
                <tr key={trace.id} className="border-b hover:bg-gray-50 transition-colors">
                  <td className="px-6 py-4 font-mono text-gray-500">{trace.id}</td>
                  <td className="px-6 py-4 font-medium text-gray-900">{trace.name}</td>
                  <td className="px-6 py-4">
                    {trace.status === "success" 
                      ? <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-800">OK</span>
                      : <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-800">ERR</span>}
                  </td>
                  <td className="px-6 py-4 text-gray-600">{trace.latency}</td>
                  <td className="px-6 py-4 text-gray-600">{trace.tokens > 0 ? trace.tokens : '-'}</td>
                  <td className="px-6 py-4 text-gray-600">{trace.cost}</td>
                  <td className="px-6 py-4 text-gray-500">{trace.time}</td>
                  <td className="px-6 py-4 text-right">
                    <button 
                      onClick={() => setSelectedTrace(trace)}
                      className="text-indigo-600 hover:text-indigo-900 text-xs font-medium"
                    >
                      View Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Bottom Section: Trace Explorer Modal */}
      {selectedTrace && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-4xl max-h-[90vh] flex flex-col overflow-hidden animate-in fade-in zoom-in-95 duration-200">
            <div className="px-6 py-4 border-b border-gray-200 flex justify-between items-center bg-gray-50">
              <div>
                <h3 className="font-semibold text-gray-900">Trace Explorer: {selectedTrace.name}</h3>
                <p className="text-xs text-gray-500 mt-1 font-mono">{selectedTrace.id}</p>
              </div>
              <button onClick={() => setSelectedTrace(null)} className="text-gray-400 hover:text-gray-600">
                <X className="w-5 h-5" />
              </button>
            </div>
            
            <div className="p-6 overflow-y-auto flex-1">
              <div className="space-y-4 font-mono text-sm">
                <div className="border border-gray-200 rounded p-4 bg-gray-50">
                  <div className="flex items-center text-blue-700 font-bold mb-2">
                    <ChevronRight className="w-4 h-4 mr-1" />
                    [Span] workflow_execution (1.2s)
                  </div>
                  <div className="pl-6 border-l-2 border-blue-200 ml-2 space-y-3">
                    
                    <div>
                      <div className="flex items-center text-indigo-600 font-bold mb-1">
                        <ChevronRight className="w-4 h-4 mr-1" />
                        [Span] langgraph_node: SalesAgent (850ms)
                      </div>
                      <div className="pl-6 text-xs text-gray-600 bg-white p-2 rounded border border-gray-100 shadow-sm">
                        <p><span className="font-semibold">llm.model:</span> gpt-4o</p>
                        <p><span className="font-semibold">llm.token_count:</span> 450</p>
                        <p><span className="font-semibold">llm.temperature:</span> 0.2</p>
                      </div>
                    </div>

                    <div>
                      <div className="flex items-center text-green-600 font-bold mb-1">
                        <ChevronRight className="w-4 h-4 mr-1" />
                        [Span] tool: hubspot_update_deal (350ms)
                      </div>
                      <div className="pl-6 text-xs text-gray-600 bg-white p-2 rounded border border-gray-100 shadow-sm">
                        <p><span className="font-semibold">tool.input:</span> {"{\"deal_id\": \"123\", \"stage\": \"qualified\"}"}</p>
                        <p><span className="font-semibold">tool.output:</span> {"{\"status\": \"success\"}"}</p>
                      </div>
                    </div>

                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
