import { useQuery } from "@tanstack/react-query";
import { agentsQuery } from "../../api/queries";

/** A map from agent ID to agent name. Deleted agents show their ID. */
export function useAgentNames(): (id: string | null) => string {
  const agents = useQuery(agentsQuery());
  const names = new Map((agents.data ?? []).map((a) => [a.id, a.name]));
  return (id) => (id === null ? "none" : (names.get(id) ?? id.slice(0, 8)));
}

/** A US dollar amount as text. Small amounts keep their precision. */
export function formatUsd(value: number): string {
  if (value === 0) return "$0";
  if (Math.abs(value) < 0.01) return `$${value.toPrecision(2)}`;
  return `$${value.toFixed(2)}`;
}

/** A rate from 0 to 1 as a percentage, or "–". */
export function formatRate(value: number | null | undefined): string {
  return value === null || value === undefined
    ? "–"
    : `${String(Math.round(value * 100))}%`;
}

/** An ISO time as local text, or "–". */
export function when(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "–";
}
