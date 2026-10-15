export type TimelineDocument = {name: string; content: string};

export async function chooseJsonExportDirectory(): Promise<FileSystemDirectoryHandle | null> {
  const picker = (globalThis as unknown as {showDirectoryPicker?: (options: unknown) => Promise<FileSystemDirectoryHandle>}).showDirectoryPicker;
  if (!picker) throw new Error('当前浏览器不支持选择保存文件夹，请通过 HTTPS 或 localhost 使用 Chrome / Edge。');
  try { return await picker({id: 'datamark-json-export', mode: 'readwrite'}); }
  catch (error) { if (error instanceof DOMException && error.name === 'AbortError') return null; throw error; }
}

/** Validate the entire batch before touching the selected folder, then verify each write. */
export async function saveJsonDocuments(directory: FileSystemDirectoryHandle, documents: TimelineDocument[]): Promise<number> {
  if (!Array.isArray(documents) || !documents.length) throw new Error('没有可导出的时间轴 JSON。');
  const names = new Set<string>();
  for (const document of documents) {
    if (!document || typeof document.name !== 'string' || !/^[^<>:"/\\|?*\u0000-\u001f]+\.json$/i.test(document.name) || names.has(document.name.toLowerCase())) throw new Error('导出文件名无效或重复，未写入文件。');
    names.add(document.name.toLowerCase());
    if (typeof document.content !== 'string') throw new Error('导出 JSON 内容无效，未写入文件。');
    JSON.parse(document.content);
  }
  let completed = 0;
  for (const document of documents) {
    try {
      const file = await directory.getFileHandle(document.name, {create: true});
      const writer = await file.createWritable();
      try { await writer.write(document.content); await writer.close(); }
      catch (error) { await writer.abort().catch(() => {}); throw error; }
      if (await (await file.getFile()).text() !== document.content) throw new Error('写入后的内容核验失败');
      completed++;
    } catch (error) {
      throw new Error(`已保存并核验 ${completed}/${documents.length} 个 JSON；${document.name} 保存失败：${error instanceof Error ? error.message : String(error)}。请检查文件夹权限和剩余空间后重试。`);
    }
  }
  return completed;
}
