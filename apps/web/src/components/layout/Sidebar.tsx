"use client";

import React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { 
  LayoutDashboard, 
  Cpu, 
  Brain, 
  CheckSquare, 
  BookOpen, 
  Plug, 
  ShieldCheck,
  BarChart3,
  Settings,
  Sparkles,
  ChevronLeft,
  ChevronRight
} from 'lucide-react';

interface SidebarProps {
  isOpen: boolean;
  setIsOpen: (val: boolean) => void;
}

const navItems = [
  { name: 'Command Center', href: '/dashboard', icon: LayoutDashboard },
  { name: 'AI Operations', href: '/dashboard/operations', icon: Cpu },
  { name: 'Intelligence', href: '/dashboard/intelligence', icon: Brain },
  { name: 'Approval Center', href: '/dashboard/approvals', icon: CheckSquare },
  { name: 'Knowledge Base', href: '/dashboard/knowledge', icon: BookOpen },
  { name: 'Integrations', href: '/dashboard/integrations', icon: Plug },
  { name: 'Audit Logs', href: '/dashboard/audit', icon: ShieldCheck },
  { name: 'Analytics', href: '/dashboard/analytics', icon: BarChart3 },
  { name: 'Settings', href: '/dashboard/settings', icon: Settings },
];

export default function Sidebar({ isOpen, setIsOpen }: SidebarProps) {
  const pathname = usePathname();

  return (
    <aside 
      className={`${isOpen ? 'w-64' : 'w-20'} bg-slate-900 text-slate-200 flex flex-col transition-all duration-300 ease-in-out shrink-0 border-r border-slate-800 z-30 shadow-xl`}
    >
      {/* Brand Header */}
      <div className="h-16 flex items-center justify-between px-4 border-b border-slate-800/80">
        <Link href="/dashboard" className="flex items-center space-x-3 overflow-hidden">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-blue-600 via-indigo-600 to-cyan-500 flex items-center justify-center text-white shadow-lg shadow-blue-500/20 shrink-0">
            <Sparkles className="h-5 w-5" />
          </div>
          {isOpen && (
            <div className="flex flex-col">
              <span className="font-bold text-base text-white tracking-tight flex items-center gap-1.5">
                TITAN <span className="text-[10px] uppercase font-mono px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 border border-blue-500/30">OS</span>
              </span>
              <span className="text-[11px] text-slate-400 font-medium">Enterprise AI Engine</span>
            </div>
          )}
        </Link>
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="p-1.5 text-slate-400 hover:text-white hover:bg-slate-800 rounded-lg transition-colors"
          title={isOpen ? "Collapse Sidebar" : "Expand Sidebar"}
        >
          {isOpen ? <ChevronLeft className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto px-3 py-4 space-y-1.5">
        {navItems.map((item) => {
          const isActive = pathname === item.href || (item.href !== '/dashboard' && pathname.startsWith(item.href));
          return (
            <Link 
              key={item.name}
              href={item.href}
              prefetch={true}
              className={`flex items-center px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150 group ${
                isActive 
                  ? 'bg-blue-600/15 text-blue-400 border border-blue-500/30 shadow-sm' 
                  : 'text-slate-400 hover:text-slate-100 hover:bg-slate-800/60 border border-transparent'
              }`}
              title={!isOpen ? item.name : undefined}
            >
              <item.icon className={`h-5 w-5 shrink-0 transition-colors ${isActive ? 'text-blue-400' : 'text-slate-400 group-hover:text-slate-200'}`} />
              {isOpen && <span className="ml-3 truncate">{item.name}</span>}
              {isOpen && isActive && (
                <span className="ml-auto w-1.5 h-1.5 rounded-full bg-blue-400 shadow-sm shadow-blue-400" />
              )}
            </Link>
          );
        })}
      </nav>

      {/* Footer Profile Status */}
      {isOpen && (
        <div className="p-3 border-t border-slate-800/80 bg-slate-950/40">
          <div className="flex items-center space-x-3 p-2 rounded-lg bg-slate-900/80 border border-slate-800">
            <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-emerald-500 to-teal-400 flex items-center justify-center text-slate-950 font-bold text-xs shadow-sm">
              JD
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-xs font-semibold text-white truncate">John Doe</p>
              <p className="text-[10px] text-emerald-400 flex items-center font-mono">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1 animate-pulse" />
                Live Agent Worker
              </p>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
