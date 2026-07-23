"use client";

import React, { useState } from 'react';
import { X, ChevronRight, ChevronLeft, Sparkles, ShieldCheck, Cpu, Database, Award, CheckCircle } from 'lucide-react';

interface TourStep {
  title: string;
  subtitle: string;
  description: string;
  icon: React.ReactNode;
  highlightTarget: string;
}

const TOUR_STEPS: TourStep[] = [
  {
    title: "Welcome to TITAN OS",
    subtitle: "Enterprise Autonomous AI Operations Platform",
    description: "TITAN orchestrates multi-agent workflows with stateful Temporal execution, zero-trust SSRF sandboxing, and Human-in-the-Loop governance.",
    icon: <Sparkles className="w-8 h-8 text-amber-400" />,
    highlightTarget: "Command Center Dashboard"
  },
  {
    title: "Executive Summary Widgets",
    subtitle: "Real-Time Enterprise Metrics",
    description: "Track total revenue, qualified lead volume, pending human approvals, active agents, tasks completed today, and CSAT scores in real time.",
    icon: <Award className="w-8 h-8 text-blue-400" />,
    highlightTarget: "Top Metric Cards"
  },
  {
    title: "Visual Analytics & Trends",
    subtitle: "30-Day Revenue & Agent Performance",
    description: "Explore interactive charts powered by Recharts showing lead conversion funnels, agent volume distribution, and activity heatmaps.",
    icon: <Cpu className="w-8 h-8 text-purple-400" />,
    highlightTarget: "Analytics Charts"
  },
  {
    title: "AI Intelligence Panel",
    subtitle: "Automated Recommendations & Risk Detection",
    description: "TITAN continuously analyzes operational logs to highlight high-value sales leads, flag security anomalies, and suggest growth opportunities.",
    icon: <Sparkles className="w-8 h-8 text-indigo-400" />,
    highlightTarget: "AI Intelligence Cards"
  },
  {
    title: "Human-in-the-Loop (HITL) Queue",
    subtitle: "Risk-Gated Governance",
    description: "High-risk actions (wire transfers, bulk outreach) are paused until an authorized human approves or rejects the action with 1-click.",
    icon: <ShieldCheck className="w-8 h-8 text-green-400" />,
    highlightTarget: "Approval Center"
  },
  {
    title: "Live Execution Traces",
    subtitle: "Deep Observability & Telemetry",
    description: "Inspect step-by-step state transitions, prompt token counts, latency breakdowns, and Pydantic validation schemas.",
    icon: <Database className="w-8 h-8 text-cyan-400" />,
    highlightTarget: "Execution Feed"
  },
  {
    title: "RAG Knowledge Base",
    subtitle: "Contextual Document Retrieval",
    description: "Vector search across company SOPs, case studies, and compliance docs to ground agent responses in ground truth.",
    icon: <Database className="w-8 h-8 text-emerald-400" />,
    highlightTarget: "Knowledge Base"
  },
  {
    title: "You're All Set!",
    subtitle: "Explore TITAN in Demo Mode",
    description: "Toggle 'Live Simulation' on the top right to watch real-time lead scoring and agent task dispatches stream live into the dashboard.",
    icon: <CheckCircle className="w-8 h-8 text-emerald-500" />,
    highlightTarget: "Live Simulation Toggle"
  }
];

interface ProductTourProps {
  isOpen: boolean;
  onClose: () => void;
}

export default function ProductTour({ isOpen, onClose }: ProductTourProps) {
  const [currentStep, setCurrentStep] = useState(0);

  if (!isOpen) return null;

  const step = TOUR_STEPS[currentStep];
  const isFirst = currentStep === 0;
  const isLast = currentStep === TOUR_STEPS.length - 1;

  const handleNext = () => {
    if (isLast) {
      onClose();
      setCurrentStep(0);
    } else {
      setCurrentStep(prev => prev + 1);
    }
  };

  const handlePrev = () => {
    if (!isFirst) {
      setCurrentStep(prev => prev - 1);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-[#1e282c] border border-gray-700 text-white rounded-xl shadow-2xl max-w-lg w-full p-6 relative overflow-hidden transition-all duration-300">
        {/* Background Glow */}
        <div className="absolute -right-12 -top-12 w-36 h-36 bg-[#3c8dbc]/20 rounded-full blur-2xl" />
        
        {/* Header */}
        <div className="flex justify-between items-center pb-4 border-b border-gray-700/80">
          <div className="flex items-center space-x-3">
            <div className="p-2 bg-[#367fa9]/30 border border-[#3c8dbc]/40 rounded-lg">
              {step.icon}
            </div>
            <div>
              <span className="text-[11px] font-bold text-[#3c8dbc] uppercase tracking-wider">
                Step {currentStep + 1} of {TOUR_STEPS.length} • {step.highlightTarget}
              </span>
              <h2 className="text-lg font-bold text-white">{step.title}</h2>
            </div>
          </div>
          <button 
            onClick={onClose}
            className="text-gray-400 hover:text-white p-1 rounded-md transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="py-5 space-y-3">
          <h3 className="text-sm font-semibold text-amber-300">{step.subtitle}</h3>
          <p className="text-sm text-gray-300 leading-relaxed">
            {step.description}
          </p>
        </div>

        {/* Progress Bar & Navigation */}
        <div className="pt-4 border-t border-gray-700/80 flex items-center justify-between">
          {/* Step Indicators */}
          <div className="flex space-x-1.5">
            {TOUR_STEPS.map((_, idx) => (
              <div 
                key={idx} 
                className={`h-1.5 rounded-full transition-all ${
                  idx === currentStep ? 'w-6 bg-[#3c8dbc]' : 'w-1.5 bg-gray-600'
                }`}
              />
            ))}
          </div>

          {/* Action Buttons */}
          <div className="flex items-center space-x-2">
            {!isFirst && (
              <button
                onClick={handlePrev}
                className="px-3 py-1.5 text-xs font-medium text-gray-300 hover:text-white bg-gray-800 hover:bg-gray-700 rounded transition-colors flex items-center"
              >
                <ChevronLeft className="w-3.5 h-3.5 mr-1" />
                Back
              </button>
            )}
            <button
              onClick={handleNext}
              className="px-4 py-1.5 text-xs font-semibold text-white bg-[#3c8dbc] hover:bg-[#367fa9] rounded transition-colors shadow-sm flex items-center"
            >
              {isLast ? "Finish Tour" : "Next"}
              {!isLast && <ChevronRight className="w-3.5 h-3.5 ml-1" />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
