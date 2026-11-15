import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useParams } from "@tanstack/react-router";
import { agentQuery } from "../api/queries";
import { useChangeEvents } from "../events";

/** The page frame: navigation and the change-event status. */
export function Shell() {
  const events = useChangeEvents();
  return (
    <div className="shell">
      <nav className="sidebar">
        <span className="brand">hagent</span>
        <Link to="/agents" activeProps={{ className: "active" }}>
          Agents
        </Link>
        <Link to="/knowledge" activeProps={{ className: "active" }}>
          Knowledge
        </Link>
        <Link to="/settings" activeProps={{ className: "active" }}>
          Settings
        </Link>
        <span className={`status ${events}`} title="Change events">
          {events === "open" ? "live" : events}
        </span>
      </nav>
      <main>
        <Outlet />
      </main>
    </div>
  );
}

/** The frame of one agent, with its tabs. */
export function AgentShell() {
  const { agentId } = useParams({ from: "/agents/$agentId" });
  const agent = useQuery(agentQuery(agentId));
  return (
    <section>
      <header className="page-header">
        <h1>{agent.data?.name ?? "Agent"}</h1>
        {agent.data && !agent.data.enabled ? (
          <span className="badge">off</span>
        ) : null}
      </header>
      <nav className="tabs">
        <Link
          to="/agents/$agentId"
          params={{ agentId }}
          activeOptions={{ exact: true }}
          activeProps={{ className: "active" }}
        >
          Definition
        </Link>
        <Link
          to="/agents/$agentId/versions"
          params={{ agentId }}
          activeProps={{ className: "active" }}
        >
          Versions
        </Link>
        <Link
          to="/agents/$agentId/test"
          params={{ agentId }}
          activeProps={{ className: "active" }}
        >
          Test
        </Link>
      </nav>
      <Outlet />
    </section>
  );
}
