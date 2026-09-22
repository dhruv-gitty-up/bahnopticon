export const PRODUCT_LABELS: Record<string, string> = {
  nationalExpress: 'ICE', national: 'IC / EC', regionalExpress: 'Regional Express',
  regional: 'Regional', suburban: 'S-Bahn', subway: 'U-Bahn', tram: 'Tram',
  bus: 'Bus', ferry: 'Ferry',
};

const dateTimeFormatter = new Intl.DateTimeFormat('de-DE', {
  day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  timeZone: 'Europe/Berlin', hourCycle: 'h23', hour12: false,
});
const timeFormatter = new Intl.DateTimeFormat('de-DE', {
  hour: '2-digit', minute: '2-digit',
  timeZone: 'Europe/Berlin', hourCycle: 'h23', hour12: false,
});
const delayNumber = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return 'Unavailable';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unavailable' : dateTimeFormatter.format(date);
}

export function formatTime(value: string | number | null | undefined): string {
  if (value == null || value === '') return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : timeFormatter.format(date);
}

export function delayLabel(delay: number | null | undefined): string {
  if (delay == null) return 'Delay unavailable';
  if (delay === 0) return 'On time';
  const minutes = Math.abs(delay);
  const duration = minutes < 1
    ? `${Math.max(1, Math.round(minutes * 60))} sec`
    : `${delayNumber.format(minutes)} min`;
  return delay > 0 ? `+${duration} late` : `${duration} early`;
}
