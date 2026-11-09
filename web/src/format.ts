export const pct = (x: number | null | undefined, digits = 1) => (x == null ? "n/a" : `${(100 * x).toFixed(digits)}%`);
export const num = (x: number) => x.toLocaleString("en-US");
export const fixed = (x: number | null | undefined, digits = 3) => (x == null ? "n/a" : x.toFixed(digits));

export function bytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
