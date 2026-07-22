export type RiskLevel = "LOW" | "MEDIUM" | "HIGH";

export interface ActionRequest {
  toolName: string;
  parameters: Record<string, any>;
  riskLevel: RiskLevel;
  reasoning: string;
  expectedOutcome: string;
}

export type ApprovalDecisionType = "APPROVED" | "REJECTED" | "EDITED";

export interface ApprovalDecision {
  actionId: string;
  decision: ApprovalDecisionType;
  modifiedParameters?: Record<string, any>;
  comments?: string;
}
