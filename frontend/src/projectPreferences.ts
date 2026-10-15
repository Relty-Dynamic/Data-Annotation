type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/** Project metadata is in SQLite; browser preferences must not break a completed operation. */
export function readProjectPreference(key: string, storage?: PreferenceStorage): string | null {
  try { return (storage ?? globalThis.localStorage).getItem(key); } catch { return null; }
}

export function readRestorableProjectPreference(lastProjectKey: string, clearedPreviewsKey: string, storage?: PreferenceStorage): string | null {
  const last = readProjectPreference(lastProjectKey, storage);
  if (!last) return null;
  try {
    if (JSON.parse(readProjectPreference(clearedPreviewsKey, storage) ?? 'null')?.id === last) {
      writeProjectPreference(lastProjectKey, null, storage);
      return null;
    }
  } catch { /* An invalid clear notice must not hide a saved project. */ }
  return last;
}

export function writeProjectPreference(key: string, value: string | null, storage?: PreferenceStorage): boolean {
  try {
    const target = storage ?? globalThis.localStorage;
    if (value === null) target.removeItem(key); else target.setItem(key, value);
    return true;
  } catch { return false; }
}

export function readRemovedProjects(key: string, storage?: PreferenceStorage): string[] {
  try {
    const value: unknown = JSON.parse(readProjectPreference(key, storage) ?? '[]');
    return Array.isArray(value) ? [...new Set(value.filter((id): id is string => typeof id === 'string'))] : [];
  } catch { return []; }
}

export function updateRemovedProjects(key: string, id: string, removed: boolean, storage?: PreferenceStorage): {ids: string[]; saved: boolean} {
  const ids = readRemovedProjects(key, storage).filter(value => value !== id);
  if (removed) ids.push(id);
  return {ids, saved: writeProjectPreference(key, JSON.stringify(ids), storage)};
}
