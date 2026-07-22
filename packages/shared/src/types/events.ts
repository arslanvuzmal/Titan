export interface TitanEvent {
  id: string;
  organizationId: string;
  source: string;
  type: string;
  payload: Record<string, any>;
  metadata: Record<string, any>;
  timestamp: string; // ISO 8601
}
