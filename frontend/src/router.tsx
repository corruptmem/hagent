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
import { Settings } from "./screens/Settings";
import { KnowledgeList } from "./screens/knowledge/KnowledgeList";
import { StoreView } from "./screens/knowledge/StoreView";
import { InspectShell } from "./screens/inspect/common";
import { Invocations } from "./screens/inspect/Invocations";
import { Trace } from "./screens/inspect/Trace";
import { RuleView, Rules } from "./screens/inspect/Rules";
import { Schedules } from "./screens/inspect/Schedules";
import { Permissions } from "./screens/inspect/Permissions";
import { Changes } from "./screens/inspect/Changes";
import { Cost } from "./screens/inspect/Cost";
import { ApprovalView, Approvals } from "./screens/inspect/Approvals";

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

const knowledgeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/knowledge",
  component: KnowledgeList,
});

const storeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/knowledge/$storeId",
  component: function StorePage() {
    const { storeId } = storeRoute.useParams();
    return <StoreView key={storeId} storeId={storeId} />;
  },
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: Settings,
});

const inspectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/inspect",
  component: InspectShell,
});

const inspectIndexRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/",
  beforeLoad: () => {
    // eslint-disable-next-line @typescript-eslint/only-throw-error
    throw redirect({ to: "/inspect/invocations" });
  },
});

const invocationsRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/invocations",
  component: Invocations,
});

const traceRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/invocations/$invocationId",
  component: function TracePage() {
    const { invocationId } = traceRoute.useParams();
    return <Trace key={invocationId} invocationId={invocationId} />;
  },
});

const rulesRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/rules",
  component: Rules,
});

const ruleRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/rules/$agentId/$ruleId",
  component: function RulePage() {
    const { agentId, ruleId } = ruleRoute.useParams();
    return <RuleView key={ruleId} agentId={agentId} ruleId={ruleId} />;
  },
});

const schedulesRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/schedules",
  component: Schedules,
});

const permissionsRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/permissions",
  component: Permissions,
});

const changesRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/changes",
  component: Changes,
});

const costRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/cost",
  component: Cost,
});

const approvalsRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/approvals",
  component: Approvals,
});

const approvalRoute = createRoute({
  getParentRoute: () => inspectRoute,
  path: "/approvals/$approvalId",
  component: function ApprovalPage() {
    const { approvalId } = approvalRoute.useParams();
    return <ApprovalView key={approvalId} approvalId={approvalId} />;
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
  knowledgeRoute,
  storeRoute,
  inspectRoute.addChildren([
    inspectIndexRoute,
    invocationsRoute,
    traceRoute,
    rulesRoute,
    ruleRoute,
    schedulesRoute,
    permissionsRoute,
    changesRoute,
    costRoute,
    approvalsRoute,
    approvalRoute,
  ]),
  settingsRoute,
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
