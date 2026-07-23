"use client";

import React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { 
  LayoutDashboard, 
  Cpu, 
  Brain, 
  CheckSquare, 
  Book, 
  Link as LinkIcon, 
  ShieldCheck,
  BarChart3,
  Settings
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
  { name: 'Knowledge Base', href: '/dashboard/knowledge', icon: Book },
  { name: 'Integrations', href: '/dashboard/integrations', icon: LinkIcon },
  { name: 'Audit Logs', href: '/dashboard/audit', icon: ShieldCheck },
  { name: 'Analytics', href: '/dashboard/analytics', icon: BarChart3 },
  { name: 'Settings', href: '/dashboard/settings', icon: Settings },
];

export default function Sidebar({ isOpen }: SidebarProps) {
  const pathname = usePathname();

  return (
    <aside 
      className={`${isOpen ? 'w-64' : 'w-20'} bg-[#222d32] text-white flex flex-col transition-all duration-300 ease-in-out shrink-0`}
    >
      {/* Brand Header */}
      <div className="h-14 flex items-center justify-center bg-[#367fa9] text-xl font-bold tracking-wider cursor-pointer">
        {isOpen ? (
          <span><b>TITAN</b> OS</span>
        ) : (
          <span><b>T</b>OS</span>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-4">
        <ul className="space-y-1">
          {navItems.map((item) => {
            const isActive = pathname === item.href;
            return (
              <li key={item.name}>
                <Link 
                  href={item.href}
                  prefetch={true}
                  className={`flex items-center px-4 py-3 border-l-[3px] transition-colors ${
                    isActive 
                      ? 'bg-[#1e282c] border-[#3c8dbc] text-white' 
                      : 'border-transparent text-gray-300 hover:bg-[#1e282c] hover:text-white'
                  }`}
                  title={!isOpen ? item.name : undefined}
                >
                  <item.icon className={`h-5 w-5 ${isActive ? 'text-[#3c8dbc]' : 'text-gray-400'}`} />
                  {isOpen && <span className="ml-3 text-sm font-medium">{item.name}</span>}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </aside>
  );
}
