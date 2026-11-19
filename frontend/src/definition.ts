import type { ValidationIssue } from "./api/types";

/** Read the value at a dotted path, for example "tools.ha.level". */
export function getPath(value: unknown, path: string): unknown {
  let current: unknown = value;
  for (const part of path.split(".")) {
    if (current === null || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

/** A copy of `value` with `path` set to `next`. */
export function setPath<T>(value: T, path: string, next: unknown): T {
  const [head, ...rest] = path.split(".");
  if (head === undefined) return value;
  const source = (value ?? {}) as Record<string, unknown>;
  return {
    ...source,
    [head]: rest.length ? setPath(source[head], rest.join("."), next) : next,
  } as T;
}

/**
 * The leaf paths of a definition: each value that is not a plain object.
 * Lists, null and the free-form objects (`result.schema`,
 * `fallback.value`) are leaves.
 */
export function leafPaths(value: unknown, prefix = ""): string[] {
  const freeForm = new Set(["result.schema", "fallback.value"]);
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    freeForm.has(prefix)
  ) {
    return prefix ? [prefix] : [];
  }
  return Object.entries(value as Record<string, unknown>).flatMap(
    ([key, child]) => leafPaths(child, prefix ? `${prefix}.${key}` : key),
  );
}

/** The dotted path of a validation issue. */
export function issuePath(issue: ValidationIssue): string {
  return issue.loc.map(String).join(".");
}

/** The issues for one field: its own path and the paths under it. */
export function issuesFor(
  issues: ValidationIssue[],
  path: string,
): ValidationIssue[] {
  return issues.filter((issue) => {
    const loc = issuePath(issue);
    return loc === path || loc.startsWith(`${path}.`);
  });
}
