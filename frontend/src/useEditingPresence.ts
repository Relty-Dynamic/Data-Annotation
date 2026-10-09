import {useEffect, useRef, useState} from 'react';
import {authFetch} from './auth.ts';

type Editor = {id: string; display_name: string; role: 'admin' | 'annotator'};

export function useEditingPresence(projectId: string | undefined): {others: Editor[]; error: string} {
  const tabId = useRef(crypto.randomUUID());
  const [state, setState] = useState<{projectId: string; others: Editor[]; error: string}>({projectId: '', others: [], error: ''});
  useEffect(() => {
    if (!projectId) return;
    const id = projectId;
    let stopped = false;
    const body = JSON.stringify({tab_id: tabId.current});
    const enter = async () => {
      try {
        const response = await authFetch(`/api/projects/${id}/editing`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body, cache: 'no-store'});
        if (!response.ok) throw new Error('无法确认此项目是否有其他人正在编辑。');
        const result = await response.json() as {others: Editor[]};
        if (!stopped) setState({projectId: id, others: result.others, error: ''});
      } catch {
        if (!stopped) setState({projectId: id, others: [], error: '无法确认此项目是否有其他人正在编辑。'});
      }
    };
    void enter();
    const timer = window.setInterval(() => void enter(), 15000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      void authFetch(`/api/projects/${id}/editing`, {method: 'DELETE', headers: {'Content-Type': 'application/json'}, body, keepalive: true}).catch(() => {});
    };
  }, [projectId]);
  return state.projectId === projectId ? {others: state.others, error: state.error} : {others: [], error: ''};
}
