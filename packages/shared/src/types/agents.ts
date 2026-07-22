import { TitanEvent } from "./events";

export interface AgentContext {
  taskId: string;
  organizationId: string;
  event: TitanEvent;
  retrievedMemories: Record<string, any>[];
  retrievedDocuments: Record<string, any>[];
}

export type AgentStatus = "SUCCESS" | "FAILED" | "REQUIRES_HUMAN";

export interface AgentOutput {
  agentType: string;
  status: AgentStatus;
  summary: string;
  data: Record<string, any>;
  confidenceScore: number;
}
