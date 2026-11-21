export function formatBytes(size: number | null): string {
  if (size === null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Math.abs(size);
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const formatted = `${value.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
  return size < 0 ? `-${formatted}` : formatted;
}
