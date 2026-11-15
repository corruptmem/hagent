import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { keys } from "./api/queries";
import { wsUrl } from "./base";

interface ChangeEvent {
  topic: string;
  action: string;
  id: string;
}

export type EventsStatus = "connecting" | "open" | "closed";

/**
 * Keep the query cache current with the change-event WebSocket.
 *
 * An event says only what changed, so the hook invalidates the matching
 * queries. After a reconnect it invalidates every query, because the app
 * does not repeat missed events (docs/api.md, "Change events").
 */
export function useChangeEvents(): EventsStatus {
  const client = useQueryClient();
  const [status, setStatus] = useState<EventsStatus>("connecting");

  useEffect(() => {
    if (typeof WebSocket === "undefined") return;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay = 1000;
    let stopped = false;
    let first = true;

    const connect = () => {
      setStatus("connecting");
      socket = new WebSocket(wsUrl("api/events"));
      socket.onmessage = (message: MessageEvent<string>) => {
        let event: ChangeEvent;
        try {
          event = JSON.parse(message.data) as ChangeEvent;
        } catch {
          return;
        }
        if (event.topic === "events" && event.action === "ready") {
          setStatus("open");
          delay = 1000;
          if (!first) void client.invalidateQueries();
          first = false;
        } else if (event.topic === "agents") {
          void client.invalidateQueries({ queryKey: keys.agents });
          // The store list shows which agents attach each store.
          void client.invalidateQueries({ queryKey: keys.stores });
        } else if (event.topic === "knowledge") {
          void client.invalidateQueries({ queryKey: keys.knowledge });
        } else if (event.topic === "settings") {
          void client.invalidateQueries({ queryKey: keys.settings });
        } else if (event.topic === "invocations") {
          // New and finished invocations change the lists, the traces and
          // the cost.
          void client.invalidateQueries({ queryKey: keys.inspect });
        } else if (event.topic === "rules") {
          void client.invalidateQueries({ queryKey: keys.rules });
        } else if (event.topic === "approvals") {
          void client.invalidateQueries({ queryKey: keys.approvals });
        } else if (event.topic === "tools") {
          void client.invalidateQueries({ queryKey: keys.inspect });
        }
      };
      socket.onclose = () => {
        setStatus("closed");
        if (stopped) return;
        timer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 30_000);
      };
    };
    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, [client]);

  return status;
}
