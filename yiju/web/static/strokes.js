const svgNS = "http://www.w3.org/2000/svg";
const han = /\p{Script=Han}/u;
const pendingLimit = 400;
const parallel = 4;
const glyphScale = 1.15;

const requests = new Map();
const ready = new Map();
const waiting = [];
let loading = 0;
let script = null;
let active = null;

function writerSource() {
  return document.querySelector('meta[name="hanzi-writer"]')?.content;
}

function library() {
  if (window.HanziWriter) return Promise.resolve(window.HanziWriter);
  if (!script) {
    script = new Promise((resolve, reject) => {
      const element = document.createElement("script");
      element.src = writerSource();
      element.addEventListener("load", () => resolve(window.HanziWriter));
      element.addEventListener("error", () => {
        script = null;
        element.remove();
        reject(new Error("Stroke order is unavailable."));
      });
      document.head.append(element);
    });
  }
  return script;
}

async function fetchCharacter(character) {
  if (loading >= parallel)
    await new Promise((resolve) => waiting.push({ character, resolve }));
  loading += 1;
  try {
    const response = await fetch(
      `/strokes/characters/${encodeURIComponent(character)}`,
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(data.error || "Stroke order is unavailable.");
    return data;
  } finally {
    loading -= 1;
    waiting.shift()?.resolve();
  }
}

function load(character) {
  if (!requests.has(character)) {
    const request = fetchCharacter(character).then((data) => {
      ready.set(character, data);
      return data;
    });
    request.catch(() => requests.delete(character));
    requests.set(character, request);
  }
  return requests.get(character);
}

export function prefetch(text) {
  for (const character of new Set(text)) {
    if (han.test(character)) load(character).catch(() => {});
  }
}

export function glyph(character) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = "glyph";
  element.dataset.strokes = character;
  element.setAttribute("aria-label", `Stroke order for ${character}`);
  element.textContent = character;
  return element;
}

export function glyphs(text) {
  return [...text].map((character) =>
    han.test(character) ? glyph(character) : character,
  );
}

function draw(element, data) {
  element.classList.remove("stroke-pending", "stroke-fallback");
  if (element.querySelector(".stroke-glyph")) return;
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class", "stroke-glyph");
  svg.setAttribute("viewBox", "0 0 1024 1024");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const group = document.createElementNS(svgNS, "g");
  group.setAttribute("transform", "translate(0 900) scale(1 -1)");
  for (const stroke of data.strokes) {
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", stroke);
    group.append(path);
  }
  svg.append(group);
  element.append(svg);
  element.classList.add("stroke-ready");
}

let warmed = false;

function warm() {
  if (warmed || !writerSource()) return;
  warmed = true;
  const start = () => library().catch(() => (warmed = false));
  if ("requestIdleCallback" in window) requestIdleCallback(start);
  else setTimeout(start, 200);
}

export function render(root) {
  if (root.querySelector(".glyph")) warm();
  for (const element of root.querySelectorAll(
    ".glyph:not(.stroke-ready, .stroke-fallback)",
  )) {
    const character = element.dataset.strokes;
    const data = ready.get(character);
    if (data) {
      draw(element, data);
      continue;
    }
    element.classList.add("stroke-pending");
    const queued = waiting.findIndex((entry) => entry.character === character);
    if (queued > 0) waiting.unshift(...waiting.splice(queued, 1));
    load(character).then(
      (value) => draw(element, value),
      () => element.classList.remove("stroke-pending"),
    );
    setTimeout(() => {
      if (!element.classList.contains("stroke-pending")) return;
      element.classList.remove("stroke-pending");
      element.classList.add("stroke-fallback");
    }, pendingLimit);
  }
}

export function enhance(root) {
  for (const container of root.querySelectorAll("[data-strokes-text]")) {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) =>
        node.parentElement.closest("rt, .reading, .glyph") ||
        !han.test(node.data)
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_ACCEPT,
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) node.replaceWith(...glyphs(node.data));
    container.removeAttribute("data-strokes-text");
  }
  render(root);
}

export function stop() {
  const previous = active;
  active = null;
  if (!previous) return;
  previous.element.classList.remove("stroke-loading", "stroke-animating");
  previous.overlay.remove();
}

function settle(request) {
  active = null;
  const { element, overlay } = request;
  const glyph = element.querySelector(".stroke-glyph");
  element.classList.remove("stroke-animating");
  if (!glyph) {
    overlay.remove();
    return;
  }
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    overlay.remove();
  };
  glyph.addEventListener("transitionend", finish, { once: true });
  setTimeout(finish, 250);
}

async function play(element) {
  const character = element.dataset.strokes;
  stop();
  const overlay = document.createElement("span");
  overlay.className = "stroke-overlay";
  overlay.setAttribute("aria-hidden", "true");
  const request = { element, overlay };
  active = request;
  element.classList.add("stroke-loading");
  try {
    const [HanziWriter, data] = await Promise.all([library(), load(character)]);
    if (active !== request) return;
    draw(element, data);
    const color = getComputedStyle(element).color;
    element.append(overlay);
    const size =
      overlay.getBoundingClientRect().width ||
      Number.parseFloat(getComputedStyle(element).fontSize) * glyphScale;
    const writer = HanziWriter.create(overlay, character, {
      width: size,
      height: size,
      padding: 0,
      showCharacter: false,
      showOutline: false,
      strokeColor: color,
      radicalColor: color,
      strokeAnimationSpeed: 1.15,
      delayBetweenStrokes: 140,
      charDataLoader: (_character, done) => done(data),
    });
    element.classList.remove("stroke-loading");
    element.classList.add("stroke-animating");
    await writer.animateCharacter();
    if (active !== request) return;
    settle(request);
  } catch {
    if (active !== request) return;
    stop();
    element.classList.add("stroke-unavailable");
    setTimeout(() => element.classList.remove("stroke-unavailable"), 600);
  }
}

document.addEventListener("click", (event) => {
  const element = event.target.closest(".glyph");
  if (!element || element.closest("[data-strokes-locked]")) return;
  const selection = getSelection();
  if (
    selection &&
    !selection.isCollapsed &&
    selection.containsNode(element, true)
  )
    return;
  event.preventDefault();
  if (active?.element === element) stop();
  else play(element);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") stop();
});
