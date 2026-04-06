export default function WaveTopNav({ tab, onTab }) {
  const tabs = [
    { id: "dashboard", label: "Dashboard" },
    { id: "map-editor", label: "Map Editor" },
    { id: "analysis", label: "Analysis Report" },
  ]
  return (
    <nav className="wave-topnav" aria-label="WAVE Fleet Planner">
      <div className="wave-topnav__brand">
        <span className="wave-topnav__logo" aria-hidden />
        <div>
          <div className="wave-topnav__title">WAVE Fleet Planner</div>
          <div className="wave-topnav__tag">FAB · ICS · Graph routing</div>
        </div>
      </div>
      <div className="wave-topnav__tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`wave-topnav__tab${tab === t.id ? " wave-topnav__tab--active" : ""}`}
            onClick={() => onTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
    </nav>
  )
}
