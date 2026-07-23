// Indian short-scale formatting for option-chain OI figures.
//
// Open interest and change-in-OI run into the crores, which read poorly as raw grouped
// integers. Each value is scaled to its own unit: >= 1 crore -> "x.xxCr", >= 1 lakh ->
// "xx.xL", and anything smaller stays a grouped integer (e.g. "45,200"). The sign is
// preserved so a change-in-OI reads "-24.3L" / "1.10Cr".
const CR = 10_000_000;
const L = 100_000;

/** Format an OI / change-in-OI count in lakh/crore units. */
export function fmtOi(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs >= CR) return `${sign}${(abs / CR).toFixed(2)}Cr`;
  if (abs >= L) return `${sign}${(abs / L).toFixed(1)}L`;
  return `${sign}${abs.toLocaleString()}`;
}
