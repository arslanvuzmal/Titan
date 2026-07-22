"use client";

import React from 'react';
import { Database, Server, Cpu, HardDrive } from 'lucide-react';

const systems = [
  { name: 'FastAPI Gateway', status: 'operational', latency: '24ms', icon: Server, color: 'text-blue-500' },
  { name: 'Temporal.io', status: 'operational', latency: '45ms', icon: Cpu, color: 'text-purple-500' },
  { name: 'PostgreSQL', status: 'operational', latency: '12ms', icon: Database, color: 'text-blue-600' },
  { name: 'Redis Cache', status: 'operational', latency: '2ms', icon: HardDrive, color: 'text-red-500' },
  { name: 'Qdrant Vector DB', status: 'degraded', latency: '150ms', icon: Database, color: 'text-orange-500' },
];

export default function SystemHealth() {
  return (
    <div className="bg-white rounded shadow-sm">
      <div className="px-4 py-3 border-b border-gray-100 flex justify-between items-center">
        <h3 className="font-medium text-gray-800">Infrastructure Health</h3>
        <span className="flex items-center text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full">
          <span className="w-2 h-2 rounded-full bg-green-500 mr-1 animate-pulse"></span>
          All Systems Online
        </span>
      </div>
      <div className="p-4">
        <ul className="space-y-4">
          {systems.map((sys, idx) => (
            <li key={idx} className="flex items-center justify-between p-3 hover:bg-gray-50 rounded-lg transition-colors border border-transparent hover:border-gray-100">
              <div className="flex items-center space-x-3">
                <div className={`p-2 rounded-lg bg-gray-50 ${sys.color}`}>
                  <sys.icon size={20} />
                </div>
                <div>
                  <p className="text-sm font-medium text-gray-800">{sys.name}</p>
                  <p className="text-xs text-gray-500 mt-0.5">{sys.latency} latency</p>
                </div>
              </div>
              <div className="flex flex-col items-end">
                {sys.status === 'operational' ? (
                  <span className="text-xs font-medium text-green-600">Operational</span>
                ) : (
                  <span className="text-xs font-medium text-orange-600">Degraded</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
