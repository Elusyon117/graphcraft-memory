// A small Clawd for the Memory pane: pixel art in SVG, animated with SMIL (no script). Clawd bobs and blinks, and a
// thought bubble above it holds a tiny memory graph whose nodes light up one after another, as recall walks links.
// `lit`: facts were recalled for the last prompt, so the graph glows; otherwise it rests dim.

const BODY = '#D97757'
const EYE = '#1F1E1D'

// 13 x 9 pixels: x = body, a = arm, l = leg, . = empty. The eyes are drawn on top so they can blink.
const PIXELS = [
  '..xxxxxxxxx..',
  '..xxxxxxxxx..',
  '..xxxxxxxxx..',
  '..xxxxxxxxx..',
  'aaxxxxxxxxxaa',
  'aaxxxxxxxxxaa',
  '..xxxxxxxxx..',
  '..l.l...l.l..',
  '..l.l...l.l..',
]

function pixels(): string {
  const out: string[] = []
  PIXELS.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) if (row[x] !== '.') out.push(`<rect x="${x}" y="${y + 6}" width="1" height="1"/>`)
  })
  return out.join('')
}

function eye(x: number): string {
  // Open for most of the 4 s loop, shut for a blink near the end.
  return (
    `<rect x="${x}" y="8" width="1" height="2" fill="${EYE}">` +
    `<animate attributeName="height" values="2;2;0.25;2" keyTimes="0;0.9;0.95;1" dur="4s" repeatCount="indefinite"/>` +
    `<animate attributeName="y" values="8;8;8.9;8" keyTimes="0;0.9;0.95;1" dur="4s" repeatCount="indefinite"/>` +
    `</rect>`
  )
}

function node(cx: number, cy: number, begin: number, lit: boolean): string {
  const on = lit ? '#E8A87C' : '#9A9893'
  const low = lit ? 0.45 : 0.3
  const high = lit ? 1 : 0.6
  return (
    `<circle cx="${cx}" cy="${cy}" r="1.1" fill="${on}" opacity="${low}">` +
    `<animate attributeName="opacity" values="${low};${high};${low}" dur="2.4s" begin="${begin}s" repeatCount="indefinite"/>` +
    `</circle>`
  )
}

export function clawdSvg(lit: boolean): string {
  const line = lit ? '#C9A48A' : '#8A8883'
  return [
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -1 30 16" width="60" height="32" shape-rendering="crispEdges">',
    // Clawd, bobbing.
    '<g>',
    '<animateTransform attributeName="transform" type="translate" values="0 0;0 -0.6;0 0" dur="2.4s" repeatCount="indefinite"/>',
    `<g fill="${BODY}">${pixels()}</g>`,
    eye(4),
    eye(8),
    '</g>',
    // Thought bubble: two puffs rising to a little memory graph.
    '<g shape-rendering="geometricPrecision">',
    `<circle cx="14" cy="5" r="0.6" fill="${line}"/>`,
    `<circle cx="16" cy="3.4" r="0.8" fill="${line}"/>`,
    `<g stroke="${line}" stroke-width="0.4" fill="none">`,
    '<line x1="20" y1="4" x2="25" y2="1.5"/><line x1="20" y1="4" x2="26" y2="6.5"/><line x1="25" y1="1.5" x2="26" y2="6.5"/>',
    '</g>',
    node(20, 4, 0, lit),
    node(25, 1.5, 0.4, lit),
    node(26, 6.5, 0.8, lit),
    '</g>',
    '</svg>',
  ].join('')
}
