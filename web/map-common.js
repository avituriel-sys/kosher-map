// Shared between web/index.html (public map) and web/admin.html (admin
// tool) so both render businesses identically by construction, not by
// two files being kept in sync by hand.

// Fork+knife is the deliberately broad default for "somewhere you eat"
// - a more specific icon (coffee cup, pizza slice, burger) makes an
// implicit claim about the business that's only as reliable as our own
// category_canonical mapping, which is a scrape-and-guess, not a
// verified fact. Only pulled out into their own icon where the claim is
// safe: bakeries (still conceptually "eating out", but croissant reads
// better and is unambiguous), supermarkets/shops (a cart makes no claim
// about what's sold), and hotels (a bed is just "this is a hotel").
const CATEGORY_ICONS_MAP = {
  restaurant: "🍴", cafe: "🍴", pizzeria: "🍴", falafel_shawarma: "🍴",
  sushi_asian: "🍴", ice_cream: "🍴", catering: "🍴",
  institutional_kitchen: "🍴", event_hall: "🍴",
  bakery_patisserie: "🥐",
  hotel: "🛏️",
  butcher: "🛒", fishmonger: "🛒", delicatessen: "🛒",
  grocery_supermarket: "🛒", greengrocer: "🛒", nuts_dried_fruit: "🛒",
  factory: "🛒",
  other: "📍"
};
function categoryIcon(categoryCanonical) {
  return CATEGORY_ICONS_MAP[categoryCanonical] || CATEGORY_ICONS_MAP.other;
}

const CATEGORY_HE = {
  restaurant: "מסעדה", cafe: "בית קפה", pizzeria: "פיצרייה",
  falafel_shawarma: "פלאפל/שווארמה", bakery_patisserie: "מאפייה/קונדיטוריה",
  ice_cream: "גלידה", sushi_asian: "מטבח אסייתי", butcher: "איטליז",
  fishmonger: "חנות דגים", delicatessen: "מעדנייה", grocery_supermarket: "מרכול",
  greengrocer: "ירקות ופירות", nuts_dried_fruit: "פיצוחים", catering: "קייטרינג",
  event_hall: "אולם אירועים", hotel: "בית מלון", institutional_kitchen: "מטבח מוסדי",
  factory: "מפעל", other: "אחר"
};
const SUPERVISION_HE = { regular: "רגילה", mehadrin: "מהדרין", badatz: 'בד"ץ', unknown: "לא ידוע" };

const KOSHER_COLORS = { meat: "#dc2626", dairy_parve: "#2563eb", mixed: "#f97316", unknown: "#9ca3af" };
function kosherColorKey(kosherTypeArray) {
  const types = kosherTypeArray || [];
  const hasMeat = types.includes("meat");
  const hasDairy = types.includes("dairy") || types.includes("parve");
  if (hasMeat && hasDairy) return "mixed";
  if (hasMeat) return "meat";
  if (hasDairy) return "dairy_parve";
  return "unknown";
}

// Compact, closer to Google's proportions (roughly as wide as tall,
// short tail) than the taller/narrower badge from the first pass.
//
// `ringed` draws an extra outer ring (admin UI only: marks a business
// that already has a saved correction). It has to be baked into the
// icon image itself rather than drawn as a separate MapLibre circle
// layer - a circle layer always centers on the exact point coordinate,
// but this badge's circle sits *above* that point (icon-anchor is
// "bottom", so the pin's tail tip is what's anchored to the
// coordinate), so a separately-drawn ring would end up centered on the
// tail tip instead of the circle and never actually line up.
function makeBadgeIcon(emoji, color, ringed) {
  const w = 40, h = 48;
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  const cx = w / 2, cy = 16, r = 15;

  ctx.beginPath();
  ctx.moveTo(cx - r * 0.5, cy + r * 0.82);
  ctx.lineTo(cx, h - 2);
  ctx.lineTo(cx + r * 0.5, cy + r * 0.82);
  ctx.closePath();
  ctx.fillStyle = color;
  ctx.fill();

  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fillStyle = "#fff";
  ctx.fill();
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = color;
  ctx.stroke();

  if (ringed) {
    ctx.beginPath();
    ctx.arc(cx, cy, r + 3, 0, Math.PI * 2);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#000";
    ctx.stroke();
  }

  ctx.font = `17px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(emoji, cx, cy + 1);

  return ctx.getImageData(0, 0, w, h);
}

function registerBadgeImages(map) {
  for (const colorKey of Object.keys(KOSHER_COLORS)) {
    for (const [category, emoji] of Object.entries(CATEGORY_ICONS_MAP)) {
      for (const ringed of [false, true]) {
        const id = `${colorKey}__${category}` + (ringed ? "__ring" : "");
        if (!map.hasImage(id)) map.addImage(id, makeBadgeIcon(emoji, KOSHER_COLORS[colorKey], ringed));
      }
    }
  }
}
function badgeIconId(row, overridden) {
  const category = row.category_canonical in CATEGORY_ICONS_MAP ? row.category_canonical : "other";
  return `${kosherColorKey(row.kosher_type)}__${category}` + (overridden ? "__ring" : "");
}

// Businesses that share (near-)identical coordinates - typically a
// mall, an office building, or any address where the geocoder can only
// resolve to the building as a whole - get spread into a small rosette
// once you're zoomed in close enough to matter. Without this they sit
// exactly on top of each other and only the topmost one is visible or
// clickable, no matter how far you zoom (zooming in never changes the
// pixel distance between two points with identical lat/lng). Mutates
// each row with _lat/_lng - the coordinates to render at; row.lat/lng
// (the real, unmodified values) are left untouched.
function spreadCoincidentPoints(rows) {
  const groups = new Map();
  for (const r of rows) {
    const key = r.lat.toFixed(5) + "," + r.lng.toFixed(5); // ~1m buckets
    (groups.get(key) || groups.set(key, []).get(key)).push(r);
  }
  const METERS_PER_DEG_LAT = 111320;
  const SPREAD_RADIUS_METERS = 14;
  for (const group of groups.values()) {
    if (group.length === 1) {
      group[0]._lat = group[0].lat;
      group[0]._lng = group[0].lng;
      continue;
    }
    const metersPerDegLng = METERS_PER_DEG_LAT * Math.cos(group[0].lat * Math.PI / 180);
    group.forEach((r, i) => {
      const angle = (2 * Math.PI * i) / group.length;
      r._lat = r.lat + (SPREAD_RADIUS_METERS * Math.sin(angle)) / METERS_PER_DEG_LAT;
      r._lng = r.lng + (SPREAD_RADIUS_METERS * Math.cos(angle)) / metersPerDegLng;
    });
  }
  return rows;
}

// Adds the three standard layers (clusters, cluster count, badge pins +
// side name label) to a GeoJSON source already on the map. Both pages
// wire their own click handlers separately (the public map opens a
// popup; the admin map selects the business into the edit form), but
// the visual layers themselves are identical.
function addBusinessLayers(map, sourceId, opts) {
  const idPrefix = opts && opts.idPrefix ? opts.idPrefix : "";

  map.addLayer({
    id: idPrefix + "clusters",
    type: "circle",
    source: sourceId,
    filter: ["has", "point_count"],
    paint: {
      "circle-color": "#6d28d9",
      "circle-opacity": 0.85,
      "circle-radius": ["step", ["get", "point_count"], 16, 20, 22, 100, 28],
      "circle-stroke-width": 2,
      "circle-stroke-color": "#fff"
    }
  });
  map.addLayer({
    id: idPrefix + "cluster-count",
    type: "symbol",
    source: sourceId,
    filter: ["has", "point_count"],
    layout: {
      "text-field": "{point_count_abbreviated}",
      "text-size": 13,
      "text-font": ["Noto Sans Bold"]
    },
    paint: { "text-color": "#fff" }
  });

  map.addLayer({
    id: idPrefix + "points-badge",
    type: "symbol",
    source: sourceId,
    filter: ["!", ["has", "point_count"]],
    layout: {
      "icon-image": ["get", "icon_id"],
      "icon-size": 0.85,
      "icon-anchor": "bottom",
      "icon-allow-overlap": true
    }
  });

  // Google-style: name beside the pin (left or right, whichever avoids
  // collisions), bold, not below it - only once zoomed in close enough
  // that showing every name wouldn't just be noise.
  map.addLayer({
    id: idPrefix + "points-label",
    type: "symbol",
    source: sourceId,
    filter: ["!", ["has", "point_count"]],
    minzoom: 15.5,
    layout: {
      "text-field": ["get", "name_raw"],
      "text-font": ["Noto Sans Bold"],
      "text-size": 12,
      "text-variable-anchor": ["left", "right"],
      "text-radial-offset": 1.0,
      "text-justify": "auto",
      "text-allow-overlap": false,
      "text-optional": true
    },
    paint: {
      "text-color": "#1f2937",
      "text-halo-color": "#fff",
      "text-halo-width": 1.4
    }
  });
}
