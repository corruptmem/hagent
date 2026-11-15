import { useQuery } from "@tanstack/react-query";
import { Link, Outlet } from "@tanstack/react-router";
import { agentsQuery } from "../../api/queries";

/** The frame of the inspect area, with its tabs (design section 7.2). */
export function InspectShell() {
  const tab = { activeProps: { className: "active" } };
  return (
    <section>
      <header className="page-header">
        <h1>Inspect</h1>
      </header>
      <nav className="tabs">
        <Link to="/inspect/invocations" {...tab}>
          Invocations
        </Link>
        <Link to="/inspect/rules" {...tab}>
          Rules
        </Link>
        <Link to="/inspect/schedules" {...tab}>
          Schedules
        </Link>
        <Link to="/inspect/permissions" {...tab}>
          Permissions
        </Link>
        <Link to="/inspect/changes" {...tab}>
          Changes to HA
        </Link>
        <Link to="/inspect/cost" {...tab}>
          Cost
        </Link>
        <Link to="/inspect/approvals" {...tab}>
          Approvals
        </Link>
      </nav>
      <Outlet />
    </section>
  );
}

/** A select of the live agents. `allowAll` adds an "All agents" choice. */
export function AgentSelect({
  value,
  onChange,
  allowAll = false,
}: {
  value: string;
  onChange: (id: string) => void;
  allowAll?: boolean;
}) {
  const agents = useQuery(agentsQuery());
  return (
    <label className="field inline">
      <span>Agent</span>
      <select
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
        }}
      >
        {allowAll ? <option value="">All agents</option> : null}
        {!allowAll && !value ? <option value="">Select an agent</option> : null}
        {(agents.data ?? []).map((a) => (
          <option key={a.id} value={a.id}>
            {a.name}
          </option>
        ))}
      </select>
    </label>
  );
}

/** JSON data in a block. */
export function Json({ value, label }: { value: unknown; label?: string }) {
  return (
    <pre className="json" aria-label={label}>
      {value === undefined ? "" : JSON.stringify(value, null, 2)}
    </pre>
  );
}
