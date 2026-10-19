/** A bounded queue shared by all files in one preparation attempt. */
export function limitConcurrency(limit: number) {
  let active = 0;
  const waiting: Array<() => void> = [];
  return async function run<T>(work: () => Promise<T>, signal: AbortSignal): Promise<T> {
    signal.throwIfAborted();
    if (active >= limit) await new Promise<void>(resolve => waiting.push(resolve));
    else active++;
    try { signal.throwIfAborted(); return await work(); }
    finally {
      const next = waiting.shift();
      if (next) next(); else active--;
    }
  };
}
