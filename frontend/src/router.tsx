import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  redirect,
  type RouterHistory,
} from "@tanstack/react-router";
import { AgentShell, Shell } from "./screens/Shell";
import { AgentsList } from "./screens/AgentsList";
import { EditAgent, NewAgent } from "./screens/AgentEditor";
import { TestPanel } from "./screens/TestPanel";
import { Versions } from "./screens/Versions";

interface RouterContext {
  queryClient: QueryClient;
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: Shell,
  notFoundComponent: () => <p>The page does not exist.</p>,
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    // eslint-disable-next-line @typescript-eslint/only-throw-error
    throw redirect({ to: "/agents" });
  },
});

const agentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/agents",
  component: AgentsList,
});

const newAgentRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/agents/new",
  component: () => (
    <section>
      <header className="page-header">
        <h1>New agent</h1>
      </header>
      <NewAgent />
    </section>
  ),
});

const agentRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/agents/$agentId",
  component: AgentShell,
});

const agentDefinitionRoute = createRoute({
  getParentRoute: () => agentRoute,
  path: "/",
  component: function AgentDefinitionPage() {
    const { agentId } = agentDefinitionRoute.useParams();
    return <EditAgent agentId={agentId} />;
  },
});

const agentVersionsRoute = createRoute({
  getParentRoute: () => agentRoute,
  path: "/versions",
  component: function AgentVersionsPage() {
    const { agentId } = agentVersionsRoute.useParams();
    return <Versions agentId={agentId} />;
  },
});

const agentTestRoute = createRoute({
  getParentRoute: () => agentRoute,
  path: "/test",
  component: function AgentTestPage() {
    const { agentId } = agentTestRoute.useParams();
    return <TestPanel agentId={agentId} />;
  },
});

export const routeTree = rootRoute.addChildren([
  indexRoute,
  agentsRoute,
  newAgentRoute,
  agentRoute.addChildren([
    agentDefinitionRoute,
    agentVersionsRoute,
    agentTestRoute,
  ]),
]);

export function makeRouter(
  queryClient: QueryClient,
  basepath: string,
  history?: RouterHistory,
) {
  return createRouter({
    routeTree,
    basepath: basepath || "/",
    context: { queryClient },
    defaultPreload: false,
    ...(history ? { history } : {}),
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
