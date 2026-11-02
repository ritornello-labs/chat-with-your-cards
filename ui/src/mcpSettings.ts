/** Private settings bridge data; never inserted into a chat message. */
export interface McpSnapshot {
  revision: string;
  inherit: boolean;
  servers: { name: string; transport: string; enabled: boolean }[];
  blocked: string[];
}

export interface McpValue {
  key: string;
  value: string;
  keep: boolean;
}

export interface McpEditor {
  name: string;
  transport: "http" | "sse" | "stdio";
  url: string;
  command: string;
  args: string[];
  env: McpValue[];
  headers: McpValue[];
}

export interface McpResponse {
  request_id: string;
  snapshot: McpSnapshot;
  editor?: McpEditor;
  error?: string;
}
