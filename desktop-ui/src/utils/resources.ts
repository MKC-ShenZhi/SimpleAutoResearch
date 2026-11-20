import type { TaskSnapshot } from '../types';

export interface ResourceSummary {
  llmRequests: number;
  totalTokens: number;
  processInvocations: number;
  unknownCount: number;
  llmLimit: number | null;
  tokenLimit: number | null;
}

export function summarizeResources(resources?: TaskSnapshot['resources']): ResourceSummary {
  const entries = resources?.entries || [];
  let llmRequests = 0;
  let totalTokens = 0;
  let processInvocations = 0;
  let unknownCount = 0;

  for (const entry of entries) {
    if (entry.status === 'unknown') {
      unknownCount += 1;
      continue;
    }
    if (entry.status !== 'settled') continue;
    const actual = entry.actual || {};
    llmRequests += Number(actual.llm_requests || 0);
    totalTokens += Number(actual.total_tokens || 0);
    processInvocations += Number(actual.process_invocations || 0);
  }

  return {
    llmRequests,
    totalTokens,
    processInvocations,
    unknownCount,
    llmLimit: resources?.limits?.llm_requests ?? null,
    tokenLimit: resources?.limits?.total_tokens ?? null,
  };
}
