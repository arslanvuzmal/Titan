"use client";

import React, { useEffect, useState } from "react";
import { CheckCircle, Loader2, AlertTriangle, XCircle, Clock } from "lucide-react";

export type StepStatus = "pending" | "running" | "completed" | "paused" | "failed";

export interface TraceStep {
  step_number: number;
  step_name: string;
  status: StepStatus;
  payload?: unknown;
}

interface ExecutionTraceViewerProps {
  taskId: string;
  token: string;
}

export default function ExecutionTraceViewer({ taskId, token }: ExecutionTraceViewerProps) {
  const [steps, setSteps] = useState<TraceStep[]>(
    Array.from({ length: 16 }, (_, i) => ({
      step_number: i + 1,
      step_name: `Step ${i + 1}`,
      status: "pending",
    }))
  );

  const [expandedStep, setExpandedStep] = useState<number | null>(null);

  useEffect(() => {
    // Initial fetch to get any existing task state would go here via React Query
    // For this demo, we rely directly on WebSockets.

    const ws = new WebSocket(`ws://localhost:8000/api/ws?token=${token}`);

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.step_number) {
          setSteps((prev) =>
            prev.map((step) =>
              step.step_number === data.step_number
                ? { ...step, step_name: data.step_name, status: data.status, payload: data.payload }
                : step
            )
          );
        }
      } catch (err) {
        console.error("Failed to parse websocket message", err);
      }
    };

    return () => {
      ws.close();
    };
  }, [taskId, token]);

  const renderIcon = (status: StepStatus) => {
    switch (status) {
      case "pending":
        return <div className="w-6 h-6 rounded-full border-2 border-gray-300 border-dashed animate-pulse" />;
      case "running":
        return <Loader2 className="w-6 h-6 text-blue-500 animate-spin" />;
      case "completed":
        return <CheckCircle className="w-6 h-6 text-green-500" />;
      case "paused":
        return <AlertTriangle className="w-6 h-6 text-yellow-500" />;
      case "failed":
        return <XCircle className="w-6 h-6 text-red-500" />;
    }
  };

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200">
      <div className="px-6 py-4 border-b border-gray-200 flex justify-between items-center bg-gray-50 rounded-t-lg">
        <h3 className="text-lg font-semibold text-gray-800">Live Execution Trace</h3>
        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800">
          <Clock className="w-3 h-3 mr-1" /> Real-time
        </span>
      </div>
      
      <div className="p-6">
        <div className="relative border-l-2 border-gray-200 ml-3 space-y-6">
          {steps.map((step) => {
            const isPending = step.status === "pending";
            return (
              <div key={step.step_number} className="relative pl-8">
                <div className="absolute -left-[13px] top-1 bg-white">
                  {renderIcon(step.status)}
                </div>
                
                <div 
                  className={`rounded-md p-3 transition-colors ${
                    !isPending ? 'cursor-pointer hover:bg-gray-50 border border-gray-100' : 'opacity-50'
                  }`}
                  onClick={() => !isPending && setExpandedStep(expandedStep === step.step_number ? null : step.step_number)}
                >
                  <div className="flex justify-between items-center">
                    <span className={`font-medium ${isPending ? 'text-gray-400' : 'text-gray-900'}`}>
                      {step.step_number}. {step.step_name}
                    </span>
                    {step.status === "paused" && (
                      <span className="text-xs font-bold bg-yellow-100 text-yellow-800 px-2 py-1 rounded">
                        AWAITING APPROVAL
                      </span>
                    )}
                  </div>
                  
                  {expandedStep === step.step_number && step.payload && (
                    <div className="mt-3 p-3 bg-gray-900 rounded text-xs text-gray-300 font-mono overflow-x-auto">
                      <pre>{JSON.stringify(step.payload, null, 2)}</pre>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
