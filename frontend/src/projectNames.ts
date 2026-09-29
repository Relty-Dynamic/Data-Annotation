/** Names are display metadata; filenames are sanitized separately. */
export function projectNameError(value: string, required = true): string {
  if (/[\u0000-\u001f\u007f-\u009f]/u.test(value)) return '项目名称不能包含换行或控制字符。';
  if (/[\ud800-\udfff]/u.test(value)) return '项目名称包含无效字符，请重新输入。';
  const name = value.trim();
  if (!name) return required ? '请填写项目名称。' : '';
  if ([...name].length > 80) return '项目名称最多 80 个字符。';
  return '';
}

export function projectDownloadName(name: string): string {
  return name.replace(/[<>:"/\\|?*\u0000-\u001f\u007f-\u009f]/gu, '_').replace(/[. ]+$/u, '') || '项目';
}
