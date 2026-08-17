// signals_stat, Spectra stat archetype. One pushed row from the Companion
// bridge, as a hero value or a two-state beacon.
//
// Three states, because two is a lie on a dashboard: ON, OFF, and "the
// publisher has gone quiet". The panels this targets are 1-bit or 6-colour, so
// ON inverts the cell rather than going red, and every state carries its own
// glyph instead of leaning on tone alone.
//
// Size tiers: xs is the glyph plus the value, sm adds the title, md/lg add the
// freshness line, and lg adds the publisher when asked for.

const ACCENTS = {
  "accent-1": "var(--accent-1)",
  "accent-2": "var(--accent-2)",
  "accent-3": "var(--accent-3)",
  "accent-4": "var(--accent-4)",
  "accent-5": "var(--accent-5)",
};

const ICON_OK = /^ph-[a-z0-9-]+$/;

function escapeHtml(s) {
  return String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

function icon(name, fallback) {
  const clean = String(name || "").trim();
  return ICON_OK.test(clean) ? clean : fallback;
}

// What the eye reads. Deliberately not what ON-matching compares against: a
// JSON true should print as "Yes" while still matching a configured "true".
function display(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return String(value);
  return String(value);
}

function matchable(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return "";
  return String(value).trim().toLowerCase();
}

function shell(size, body, extraCss = "") {
  return `
    <link rel="stylesheet" href="/static/style/spectra-widgets.css">
    <style>
      .w[data-widget="signals_stat"] .w-body { min-width: 0; }
      .sg-head { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
      .sg-head i { font-size: 1.15em; }
      .sg-head h3 {
        margin: 0;
        font-size: var(--fs-label);
        font-weight: var(--fw-bold);
        letter-spacing: var(--ls-label);
        text-transform: var(--label-transform, uppercase);
        color: var(--text-muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .sg-hero { display: flex; align-items: center; gap: var(--space-3); min-width: 0; }
      .sg-hero .glyph { font-size: var(--fs-display); line-height: 1; flex: 0 0 auto; }
      .sg-hero .stat-value { min-width: 0; }
      .sg-hero .stat-value .txt {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .sg-meta {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--fs-caption);
        font-weight: var(--fw-semi);
        color: var(--text-secondary);
      }
      .sg-meta i { font-size: 1em; }
      .size-xs .sg-hero { gap: var(--space-2); }
      .sg-hero.sg-solo { justify-content: center; flex: 1 1 auto; }
      .sg-hero.sg-solo .glyph { font-size: min(46cqw, 46cqh); }
      ${extraCss}
    </style>
    <div class="w size-${escapeHtml(size)}" data-widget="signals_stat">
      <div class="w-body stat-body">${body}</div>
    </div>`;
}

// Non-value states (nothing published, nothing picked, gone, expired) all read
// the same way: name the state, don't fake a reading.
function notice(size, glyph, title, message) {
  const head =
    size === "xs"
      ? ""
      : `<div class="sg-head"><i class="ph-bold ${escapeHtml(glyph)}"></i><h3>${escapeHtml(title)}</h3></div>`;
  return shell(
    size,
    `${head}
     <div class="sg-hero">
       ${size === "xs" ? `<i class="ph-bold ${escapeHtml(glyph)} glyph"></i>` : ""}
       <p class="u-muted">${escapeHtml(message)}</p>
     </div>`,
  );
}

export default function render(shadow, ctx) {
  const opts = ctx?.cell?.options || {};
  const data = ctx?.data || {};
  const size = ctx?.cell?.size || "md";
  const fragment = ctx?.fragment || "full";
  const accent = ACCENTS[opts.accent] || ACCENTS["accent-1"];
  // Every accent ships a low-saturation companion token; a wash built from that
  // dithers cleanly, where a hand-mixed transparency doesn't.
  const soft = accent.replace(/\)$/, "-soft)");

  if (data.state === "unpublished" || data.state === "unconfigured") {
    shadow.innerHTML = notice(size, "ph-broadcast", "Signals", data.message || "No signal.");
    return;
  }
  if (data.state === "missing" || data.state === "expired") {
    const title = String(opts.title || "").trim() || "Signal";
    const message =
      data.state === "expired"
        ? "The last snapshot expired."
        : data.message || "That signal isn't published.";
    shadow.innerHTML = notice(size, "ph-question", title, message);
    return;
  }

  const onValues = String(opts.on_values ?? "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  const mode = String(opts.mode || "auto");
  const isBeacon = mode === "beacon" || (mode === "auto" && onValues.length > 0);

  // State first, value second: a publisher that sends state names shouldn't
  // need its values to also happen to spell "on".
  const rowState = matchable(data.row_state);
  const rowValue = matchable(data.value);
  const isOn = onValues.includes(rowState) || onValues.includes(rowValue);

  const stale = data.state === "stale";
  const title = String(opts.title || "").trim() || String(data.label || "Signal");
  const unit = opts.show_unit === false ? "" : String(data.unit || "").trim();

  const glyph = isBeacon
    ? icon(isOn ? opts.on_icon : opts.off_icon, isOn ? "ph-bell-ringing" : "ph-check-circle")
    : "ph-broadcast";
  const text = isBeacon
    ? String((isOn ? opts.on_text : opts.off_text) ?? (isOn ? "On" : "Off"))
    : display(data.value);

  // How loudly ON reads scales with the cell. A small tile is a badge, so it
  // flips to a full accent fill: unmistakable at a glance across a room, and
  // contrast rather than hue, which is what survives a 1-bit dither. A half- or
  // full-panel cell flooded in one colour would shout over everything beside
  // it, so md/lg get the soft accent wash and keep the accent on the glyph.
  const heavy = isBeacon && isOn && !stale && (size === "xs" || size === "sm");
  const wash = isBeacon && isOn && !stale && !heavy;
  const emphasis = heavy
    ? `
      .w[data-widget="signals_stat"] {
        background: ${accent};
        color: var(--on-accent);
      }
      .w[data-widget="signals_stat"] .sg-head h3,
      .w[data-widget="signals_stat"] .sg-meta,
      .w[data-widget="signals_stat"] .stat-value { color: var(--on-accent); }`
    : `
      ${wash ? `.w[data-widget="signals_stat"] { background: ${soft}; }` : ""}
      .w[data-widget="signals_stat"] .sg-hero .glyph { color: ${accent}; }`;

  // At xs a beacon is the glyph, full stop: "Unread" truncates to "Unr…" in a
  // 180px tile, and a clipped word reads as a broken widget rather than a
  // state. Values stay (a number fits where a word doesn't).
  const glyphOnly = isBeacon && size === "xs";

  if (fragment === "badge") {
    shadow.innerHTML = shell(
      size,
      `<div class="sg-hero">
         <i class="ph-bold ${escapeHtml(glyph)} glyph" style="font-size: var(--fs-lead)"></i>
         ${glyphOnly ? "" : `<span class="stat-caption">${escapeHtml(text)}</span>`}
       </div>`,
      emphasis,
    );
    return;
  }

  const valueEl = glyphOnly
    ? `<div class="sg-hero sg-solo"><i class="ph-bold ${escapeHtml(glyph)} glyph"></i></div>`
    : `
    <div class="sg-hero">
      <i class="ph-bold ${escapeHtml(glyph)} glyph"></i>
      <div class="stat-value">
        <span class="txt">${escapeHtml(text)}</span>
        ${unit ? `<span class="unit">${escapeHtml(unit)}</span>` : ""}
      </div>
    </div>`;

  if (fragment === "value") {
    shadow.innerHTML = shell(size, valueEl, emphasis);
    return;
  }

  const head =
    size === "xs"
      ? ""
      : `<div class="sg-head">
           <i class="ph-bold ph-broadcast"></i>
           <h3>${escapeHtml(title)}</h3>
         </div>`;

  // No elapsed counter here. A publisher only writes when its state changes, so
  // an "Xm ago" line would be the one thing on the cell that keeps moving, and
  // every tick of it costs a panel refresh for information nobody asked for. The
  // cell states what the signal is; when it went stale, it says that instead.
  const showMeta = size === "md" || size === "lg";
  const publisher =
    opts.show_publisher && size === "lg" && data.publisher
      ? escapeHtml(String(data.publisher))
      : "";
  const meta =
    showMeta && (stale || publisher)
      ? `<div class="sg-meta">
           <i class="ph-bold ${stale ? "ph-warning-circle" : "ph-broadcast"}"></i>
           <span>${[stale ? "Not reporting" : "", publisher].filter(Boolean).join(" · ")}</span>
         </div>`
      : "";

  shadow.innerHTML = shell(size, `${head}${valueEl}${meta}`, emphasis);
}
