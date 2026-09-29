import { useEffect } from 'react';

export function useBrowserLifetime() {
  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let suspended = false;
    const connect = () => {
      if (suspended || (socket && socket.readyState < WebSocket.CLOSING)) return;
      const connection = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/browser/connection`);
      socket = connection;
      connection.onclose = () => {
        if (socket !== connection) return;
        socket = null;
        if (!suspended) retry = setTimeout(connect, 1000);
      };
    };
    const leave = () => {
      suspended = true;
      clearTimeout(retry);
      const previous = socket;
      socket = null;
      previous?.close();
    };
    const resume = () => { suspended = false; connect(); };
    // pagehide fires only after any unsaved-change beforeunload prompt is accepted.
    window.addEventListener('pagehide', leave);
    window.addEventListener('pageshow', resume);
    window.addEventListener('online', resume);
    connect();
    return () => {
      leave();
      window.removeEventListener('pagehide', leave);
      window.removeEventListener('pageshow', resume);
      window.removeEventListener('online', resume);
    };
  }, []);
}
