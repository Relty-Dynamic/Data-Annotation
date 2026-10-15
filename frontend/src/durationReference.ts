/** Label durations measured leftward from the strip's right edge without overlap. */
export function durationReferenceLabels(minuteWidth: number, availableWidth: number) {
  const labelWidth = 26, gap = 5;
  if (availableWidth < labelWidth || minuteWidth <= 0) return [];
  const labels: Array<{ seconds: number; right: number }> = [];
  for (const seconds of [60, 30, 5, 10, 15, 20, 25, 35, 40, 45, 50, 55]) {
    const position = minuteWidth * seconds / 60;
    if (position > availableWidth) continue;
    const right = Math.max(0, Math.min(availableWidth - labelWidth, position - labelWidth / 2));
    if (labels.some(label => Math.abs(label.right - right) < labelWidth + gap)) continue;
    labels.push({ seconds, right });
  }
  return labels;
}
