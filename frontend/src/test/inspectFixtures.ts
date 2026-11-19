import type {
  ApprovalRequest,
  AuditEntry,
  InvocationSummary,
  RuleDetail,
  Trace,
} from "../api/types";
import { AGENT_ID } from "./fakeApi";

export const INV_ID = "1".repeat(32);
export const RULE_ID = "e".repeat(32);
export const APPROVAL_ID = "d".repeat(32);

export function makeInvocation(
  change: Partial<InvocationSummary> = {},
): InvocationSummary {
  return {
    id: INV_ID,
    agent_id: AGENT_ID,
    source: "invoke",
    source_key: "invoke",
    conversation_id: null,
    dry_run: false,
    created_at: "2026-11-15T10:00:00+00:00",
    started_at: "2026-11-15T10:00:00+00:00",
    finished_at: "2026-11-15T10:00:02+00:00",
    status: "ok",
    path: "agent_turn",
    fallback_reason: null,
    error_code: null,
    agent_version: 2,
    rule_id: null,
    rule_version: null,
    turn_id: "turn1",
    cost_usd: 0.0021,
    ...change,
  };
}

export function makeAudit(change: Partial<AuditEntry> = {}): AuditEntry {
  return {
    id: 1,
    created_at: "2026-11-15T10:00:01+00:00",
    finished_at: "2026-11-15T10:00:01+00:00",
    agent_id: AGENT_ID,
    agent_version: 2,
    invocation_id: INV_ID,
    tool: "ha.call_service",
    level: "control",
    request: { domain: "light", service: "turn_on" },
    decision: "allowed",
    reason_code: null,
    reason: null,
    status: "ok",
    error: null,
    context_id: null,
    previous: null,
    previous_note: null,
    ...change,
  };
}

const TOKENS = {
  input_tokens: 900,
  cache_read_tokens: 600,
  cache_write_tokens: 0,
  output_tokens: 40,
};

export function makeTrace(change: Partial<Trace> = {}): Trace {
  const denied = makeAudit({
    id: 2,
    decision: "denied",
    status: "denied",
    reason_code: "scope",
    reason: "lock.front is outside the control scope",
  });
  return {
    invocation: {
      ...makeInvocation(),
      request: { data: { room: "test room" } },
      result: "light on",
      error: null,
    },
    turns: [
      {
        id: "turn1",
        agent_version: 2,
        source: "invoke",
        dry_run: false,
        started_at: "2026-11-15T10:00:00+00:00",
        finished_at: "2026-11-15T10:00:02+00:00",
        status: "ok",
        stop_reason: null,
        error: null,
        input: { data: {} },
        prompt: "the prompt text",
        result: "light on",
        messages: [{ kind: "request", parts: [{ content: "model message" }] }],
        usage: { requests: 2, tool_calls: 2, ...TOKENS },
        tool_calls: [
          {
            seq: 1,
            created_at: "2026-11-15T10:00:01+00:00",
            tool: "ha.call_service",
            arguments: { domain: "light" },
            ok: true,
            error_code: null,
            result: { ok: true },
            result_chars: 11,
            cut: false,
            scope_decision: "allowed",
            audit: makeAudit(),
          },
          {
            seq: 2,
            created_at: "2026-11-15T10:00:01+00:00",
            tool: "ha.call_service",
            arguments: { domain: "lock" },
            ok: false,
            error_code: "scope",
            result: { error: "scope" },
            result_chars: 17,
            cut: false,
            scope_decision: "denied",
            audit: denied,
          },
        ],
        compactions: [
          {
            id: 1,
            created_at: "2026-11-15T10:00:01+00:00",
            kind: "tool_result_cut",
            detail: { seq: 1 },
            summary: null,
          },
        ],
      },
    ],
    audit: [makeAudit(), denied],
    provider_calls: [
      {
        id: 1,
        started_at: "2026-11-15T10:00:00+00:00",
        provider: "openai",
        call_type: "chat",
        model: "test-model",
        status: "ok",
        error: null,
        cost_usd: 0.0021,
        cost_source: "price_table",
        ...TOKENS,
      },
    ],
    trials: [],
    approvals: [],
    cost: { total_usd: 0.0021, ...TOKENS },
    ...change,
  };
}

export function makeRule(change: Partial<RuleDetail> = {}): RuleDetail {
  const stats = {
    created_at: "2026-11-14T10:00:00+00:00",
    declined: 0,
    cache_hits: 0,
    errors: 0,
    last_used_at: null,
    last_error: null,
  };
  return {
    id: RULE_ID,
    state: "active",
    current_version: 2,
    latest_version: 2,
    deleted: false,
    spec: { name: "warm room", match: "{{ true }}" },
    versions: [
      {
        version: 1,
        matched: 10,
        answered: 9,
        cost_usd: 0,
        ...stats,
        backtest: { runs: 1, cost_usd: 0, agreement_rate: 0.9 },
        trial: {
          runs: 0,
          agreed: 0,
          disagreed: 0,
          errors: 0,
          budget: 0,
          agreement_rate: null,
          cost_usd: 0,
        },
      },
      {
        version: 2,
        matched: 4,
        answered: 4,
        cost_usd: 0.001,
        ...stats,
        backtest: { runs: 2, cost_usd: 0, agreement_rate: 0.75 },
        trial: {
          runs: 5,
          agreed: 4,
          disagreed: 1,
          errors: 0,
          budget: 0,
          agreement_rate: 0.8,
          cost_usd: 0.004,
        },
      },
    ],
    ...change,
  };
}

export function makeApproval(
  change: Partial<ApprovalRequest> = {},
): ApprovalRequest {
  return {
    id: APPROVAL_ID,
    status: "pending",
    agent_id: AGENT_ID,
    agent_version: 2,
    invocation_id: INV_ID,
    tool: "ha.call_service",
    summary: "Agent 'Test agent' asks to run ha.call_service.",
    request: { domain: "lock", service: "unlock" },
    approvers: ["user1"],
    targets: [],
    problems: [],
    created_at: "2026-11-15T10:00:00+00:00",
    expires_at: "2026-11-15T10:05:00+00:00",
    finished_at: null,
    answered_by: null,
    answered_via: null,
    reason: null,
    ...change,
  };
}
