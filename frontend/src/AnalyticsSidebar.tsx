import { useEffect, useState } from 'react';
import type { SevenDayAnalytics } from './analytics';
import { loadSevenDayAnalytics } from './analyticsClient';

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

export function AnalyticsSidebar({ selectedRegion, onSelectRegion }: {
  selectedRegion: string;
  onSelectRegion: (region: string) => void;
}) {
  const [analytics, setAnalytics] = useState<SevenDayAnalytics | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    loadSevenDayAnalytics().then(payload => {
      if (!active) return;
      setAnalytics(payload);
      setFailed(false);
    }).catch(() => {
      if (!active) return;
      setFailed(true);
    });
    return () => { active = false; };
  }, []);

  return (
    <aside
      className="island analytics-sidebar fixed top-6 right-6 z-40"
      style={{ position: 'fixed', top: '1.5rem', right: '1.5rem', zIndex: 40 }}
      aria-label="Seven-day network analytics"
    >
      <div className="analytics-heading">
        <div>
          <p className="eyebrow">Network intelligence</p>
          <h2>7-Day Performance</h2>
        </div>
        {analytics?.isMock && <span className="mock-badge">Preview</span>}
      </div>
      {!analytics && !failed && <p className="analytics-message" role="status">Loading network analytics…</p>}
      {failed && <p className="analytics-message analytics-message--error" role="status">Analytics unavailable.</p>}
      {analytics && (
        <>
          <div className="network-kpis">
            {analytics.networkPerformance.map(row => {
              const percentage = Math.round(row.onTimeProbability * 100);
              return (
                <article className="network-kpi" key={row.product}>
                  <div><strong>{row.label}</strong><span>{percentage}% on-time</span></div>
                  <div className="kpi-track" role="progressbar" aria-label={`${row.label} on-time probability`}
                    aria-valuemin={0} aria-valuemax={100} aria-valuenow={percentage}>
                    <span style={{ width: `${percentage}%` }} />
                  </div>
                  <small>Ø delay {number.format(row.averageDelayMinutes)} min</small>
                </article>
              );
            })}
          </div>
          <div className="region-section">
            <div className="region-heading"><span className="eyebrow">Bundesländer</span><span>Regional / S-Bahn</span></div>
            <div className="region-list">
              {analytics.regionalPerformance.map(region => {
                const selected = selectedRegion === region.bundesland;
                return (
                  <button key={region.bundesland} type="button" className="region-kpi"
                    aria-pressed={selected}
                    onClick={() => onSelectRegion(selected ? '' : region.bundesland)}>
                    <strong>{region.bundesland}</strong>
                    <span><i style={{ width: `${region.regionalOnTimePercentage}%` }} />
                      RE {number.format(region.regionalOnTimePercentage)}%</span>
                    <span><i style={{ width: `${region.suburbanOnTimePercentage}%` }} />
                      S {number.format(region.suburbanOnTimePercentage)}%</span>
                  </button>
                );
              })}
            </div>
          </div>
          <p className="analytics-footnote">Click a region to highlight it on the map.</p>
        </>
      )}
    </aside>
  );
}
