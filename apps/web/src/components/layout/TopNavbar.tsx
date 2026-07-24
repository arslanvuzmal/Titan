"use client";

import React from 'react';
import { Search, Bell, HelpCircle, Shield, Sparkles } from 'lucide-react';

interface TopNavbarProps {
  sidebarOpen: boolean;
  setSidebarOpen: (val: boolean) => void;
}

export default function TopNavbar({ sidebarOpen, setSidebarOpen }: TopNavbarProps) {
  return (
    <header className="h-16 bg-white border-b border-slate-200/80 flex items-center justify-between px-6 shrink-0 z-20 shadow-xs">
      {/* Search & Global Filter */}
      <div className="flex-1 max-w-xl flex items-center space-x-3">
        <div className="relative w-full">
          <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
          <input
            type="text"
            placeholder="Search AI agents, tasks, telemetry traces, approval queues..."
            className="w-full pl-10 pr-4 py-2 bg-slate-50 border border-slate-200 rounded-lg text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all"
          />
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-[10px] font-mono text-slate-400 bg-slate-200/60 px-1.5 py-0.5 rounded border border-slate-300/50">
            ⌘K
          </span>
        </div>
      </div>

      {/* Right Controls */}
      <div className="flex items-center space-x-3">
        {/* System Health Badge */}
        <div className="hidden lg:flex items-center space-x-1.5 px-3 py-1 rounded-full bg-emerald-50 border border-emerald-200/60 text-emerald-700 text-xs font-medium">
          <Shield className="h-3.5 w-3.5 text-emerald-600" />
          <span>System Healthy</span>
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 ml-1 animate-pulse" />
        </div>

        {/* Demo Mode Badge */}
        <div className="hidden sm:flex items-center space-x-1 px-3 py-1 rounded-full bg-blue-50 border border-blue-200/60 text-blue-700 text-xs font-semibold">
          <Sparkles className="h-3.5 w-3.5 text-blue-600" />
          <span>Live Demo Active</span>
        </div>

        <div className="h-4 w-[1px] bg-slate-200 my-auto hidden sm:block" />

        {/* Notifications Button */}
        <button 
          className="relative p-2 text-slate-500 hover:text-slate-700 hover:bg-slate-100 rounded-lg transition-colors focus:outline-none"
          title="Notifications"
        >
          <Bell className="h-5 w-5" />
          <span className="absolute top-1.5 right-1.5 w-2 h-2 bg-amber-500 rounded-full ring-2 ring-white" />
        </button>

        {/* Help Center */}
        <button 
          className="p-2 text-slate-500 hover:text-slate-700 hover:bg-slate-100 rounded-lg transition-colors focus:outline-none"
          title="Help & Documentation"
        >
          <HelpCircle className="h-5 w-5" />
        </button>
      </div>
    </header>
  );
}
