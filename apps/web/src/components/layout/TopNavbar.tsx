"use client";

import React from 'react';
import { UserButton } from "@clerk/nextjs";
import { Menu, Bell } from 'lucide-react';

interface TopNavbarProps {
  sidebarOpen: boolean;
  setSidebarOpen: (val: boolean) => void;
}

export default function TopNavbar({ sidebarOpen, setSidebarOpen }: TopNavbarProps) {
  return (
    <header className="h-14 bg-[#3c8dbc] text-white flex items-center justify-between px-4 shadow shrink-0 z-10">
      <div className="flex items-center">
        <button 
          onClick={() => setSidebarOpen(!sidebarOpen)}
          className="p-1 hover:bg-[#367fa9] rounded transition-colors focus:outline-none"
        >
          <Menu className="h-5 w-5" />
        </button>
        
        {/* Breadcrumbs placeholder */}
        <div className="hidden md:block ml-4 text-sm">
          <span className="opacity-80">Home</span> <span className="opacity-60 mx-1">/</span> <span className="font-medium">Dashboard</span>
        </div>
      </div>

      <div className="flex items-center space-x-4">
        {/* Notifications */}
        <button className="relative p-1 hover:bg-[#367fa9] rounded transition-colors focus:outline-none">
          <Bell className="h-5 w-5" />
          <span className="absolute top-0 right-0 bg-[#f39c12] text-[10px] font-bold px-1 rounded-sm">
            3
          </span>
        </button>

        {/* User Profile via Clerk */}
        <div className="flex items-center bg-[#367fa9] rounded-full p-[2px]">
          <UserButton afterSignOutUrl="/sign-in" />
        </div>
      </div>
    </header>
  );
}
