// The pane's own small Clawd, drawn when the clawd-pet mod is not installed (graph/clawd.py then has no frames).
// Pixel art in SVG, animated with SMIL (no script): eased breathing, eyes that glance left and right, a blink, and,
// when facts were recalled for the last prompt (`lit`), a lightbulb that pops in and glows above his head.

const BODY = '#D97757'
const SHADE = '#BF694D'
const EYE = '#000000'

// 12 x 8 pixels, the clawd-pet proportions: x = body, s = shaded edge, a = arm, l = leg. Eyes are drawn on top.
const PIXELS = [
  '..xxxxxxxx..',
  '..xxxxxxxx..',
  'aaxxxxxxxxaa',
  'aaxxxxxxxxaa',
  '..xxxxxxxx..',
  '..ssssssss..',
  '..l.l..l.l..',
  '..l.l..l.l..',
]
const TOP = 8 // rows above Clawd, for the lightbulb
const EASE = 'keySplines="0.45 0 0.55 1;0.45 0 0.55 1" calcMode="spline"'

function sprite(): string {
  const out: string[] = []
  PIXELS.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) {
      const c = row[x]
      if (c === '.') continue
      const fill = c === 's' || c === 'l' ? SHADE : BODY
      out.push(`<rect x="${x}" y="${y + TOP}" width="1" height="1" fill="${fill}"/>`)
    }
  })
  return out.join('')
}

function eyes(): string {
  // Both eyes glance right, back, left, back over 6 s, and blink once near the end of the loop.
  const glance =
    '<animateTransform attributeName="transform" type="translate" values="0 0;0 0;1 0;1 0;0 0;0 0;-1 0;-1 0;0 0" ' +
    'keyTimes="0;0.15;0.2;0.38;0.43;0.58;0.63;0.8;0.85" calcMode="discrete" dur="6s" repeatCount="indefinite"/>'
  const blink =
    '<animate attributeName="height" values="1;1;0.15;1;1" keyTimes="0;0.9;0.92;0.95;1" dur="6s" repeatCount="indefinite"/>'
  const eye = (x: number) => `<rect x="${x}" y="${TOP + 1}" width="1" height="1" fill="${EYE}">${blink}</rect>`
  return `<g>${glance}${eye(3)}${eye(8)}</g>`
}

function bulb(): string {
  // Pops in (scale 0 -> 1.15 -> 1), then glows softly; rays pulse with the glow.
  const glow = `<animate attributeName="opacity" values="0.75;1;0.75" dur="1.6s" begin="0.4s" repeatCount="indefinite" ${EASE}/>`
  return [
    '<g transform="translate(6 3.6)">',
    '<g transform="scale(0)">',
    '<animateTransform attributeName="transform" type="scale" values="0;1.15;1" keyTimes="0;0.7;1" dur="0.4s" fill="freeze"/>',
    `<g>${glow}`,
    '<circle cx="0" cy="0" r="2.1" fill="#F2C14E"/>',
    '<circle cx="-0.6" cy="-0.6" r="0.7" fill="#FFF3C4"/>',
    '<rect x="-0.9" y="1.9" width="1.8" height="1" fill="#9A9893"/>',
    '<g stroke="#F2C14E" stroke-width="0.35" stroke-linecap="round">',
    '<line x1="-3.4" y1="0" x2="-2.8" y2="0"/><line x1="2.8" y1="0" x2="3.4" y2="0"/><line x1="0" y1="-3.4" x2="0" y2="-2.8"/>',
    '<line x1="-2.4" y1="-2.4" x2="-2" y2="-2"/><line x1="2.4" y1="-2.4" x2="2" y2="-2"/>',
    '</g></g></g></g>',
  ].join('')
}

export function clawdSvg(lit: boolean): string {
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 ${TOP + 8}" width="64" height="40">`,
    '<g shape-rendering="crispEdges">',
    // Breathing: a half-pixel rise and fall, eased, so the motion reads smooth at any size.
    `<animateTransform attributeName="transform" type="translate" values="0 0;0 -0.5;0 0" keyTimes="0;0.5;1" dur="2.4s" repeatCount="indefinite" ${EASE}/>`,
    sprite(),
    eyes(),
    '</g>',
    lit ? `<g shape-rendering="geometricPrecision">${bulb()}</g>` : '',
    '</svg>',
  ].join('')
}
