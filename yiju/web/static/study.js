import * as strokes from "./strokes.js";

const outboxKey = "yiju-outbox";
const ratings = [
  [1, "Again"],
  [2, "Hard"],
  [3, "Good"],
  [4, "Easy"],
];
const thumbGap = 6;

const state = {
  queue: [],
  count: 0,
  fresh: 0,
  ratedSinceFetch: 0,
  current: null,
  revealed: false,
  lastReview: null,
  learnedAt: { stability: 7, days: 3 },
  outbox: [],
  consumed: new Set(),
  flags: new Map(),
  marked: new Set(),
  autoplay: !document.cookie.split("; ").includes("yiju-audio=off"),
  failure: null,
  saving: null,
  fetching: null,
  generation: 0,
  session: { reviewed: 0, ratings: { 1: 0, 2: 0, 3: 0, 4: 0 } },
};

let dom = null;
let listeners = null;
let player = null;
let pad = null;

try {
  state.outbox = JSON.parse(sessionStorage.getItem(outboxKey) || "[]");
} catch {
  state.outbox = [];
}

function csrf() {
  return document.querySelector('meta[name="csrf"]')?.content || "";
}

function h(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") element.className = value;
    else if (name === "dataset") Object.assign(element.dataset, value);
    else if (name.startsWith("on"))
      element.addEventListener(name.slice(2), value);
    else element.setAttribute(name, value === true ? "" : value);
  }
  element.append(
    ...children
      .flat()
      .filter(
        (child) => child !== null && child !== undefined && child !== false,
      ),
  );
  return element;
}

function icon(name) {
  return document
    .querySelector("#icons")
    .content.querySelector(`[data-icon="${name}"] svg`)
    .cloneNode(true);
}

function key(card) {
  return `${card.id}:${card.version}`;
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function hanzi(reading) {
  const element = h("span", { class: "hanzi", lang: "zh-Hans" });
  if (reading.length === 1 && reading[0][0].length > 4 && reading[0][1]) {
    element.append(
      h("span", { class: "reading" }, reading[0][1]),
      ...strokes.glyphs(reading[0][0]),
    );
    return element;
  }
  const punctuation = (segment) =>
    !segment[1] && /^[\p{P}\s]+$/u.test(segment[0]);
  let start = 0;
  let end = reading.length;
  while (start < end && punctuation(reading[start])) start += 1;
  while (end > start && punctuation(reading[end - 1])) end -= 1;
  const hang = (segments, side) =>
    segments.length
      ? h(
          "span",
          { class: `hang hang-${side}` },
          ...segments.flatMap(([text]) => strokes.glyphs(text)),
        )
      : null;
  const lead = hang(reading.slice(0, start), "start");
  if (lead) element.append(lead);
  for (const [text, syllable] of reading.slice(start, end)) {
    if (syllable)
      element.append(
        h("ruby", {}, ...strokes.glyphs(text), h("rt", {}, syllable)),
      );
    else element.append(...strokes.glyphs(text));
  }
  const trail = hang(reading.slice(end), "end");
  if (trail) element.append(trail);
  return element;
}

function svg(tag, attributes = {}, ...children) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes))
    if (value !== null && value !== undefined)
      element.setAttribute(name, value);
  element.append(...children.filter(Boolean));
  return element;
}

function unstudied(title) {
  return h(
    "span",
    { class: "strength", title },
    h("span", { class: "strength-label" }, "New"),
  );
}

function meter({ label, stability, value, learned, summary, extra }) {
  const fill = Math.min(1, stability / state.learnedAt.stability) * 100;
  return h(
    "span",
    { class: `strength${learned ? " learned" : ""}`, title: summary },
    h("span", { class: "sr-only" }, summary),
    h("span", { class: "strength-label", "aria-hidden": "true" }, label),
    svg(
      "svg",
      {
        class: "strength-bar",
        viewBox: "0 0 100 6",
        preserveAspectRatio: "none",
        "aria-hidden": "true",
      },
      svg("rect", { class: "strength-track", width: 100, height: 6, rx: 3 }),
      fill > 0
        ? svg("rect", {
            class: "strength-fill",
            width: Math.max(fill, 6),
            height: 6,
            rx: 3,
          })
        : null,
    ),
    h("span", { class: "strength-value", "aria-hidden": "true" }, value),
    extra,
  );
}

function recallNote(recall) {
  return recall === null ? "." : `, ${recall}% recall now.`;
}

function strength(word) {
  if (word.status === "unlocked" || word.status === "locked")
    return unstudied("Not studied yet");
  const goal = state.learnedAt;
  const { stability, days, recall } = word.strength;
  const learned = word.status === "learned";
  const label = learned ? "Learned" : "Learning";
  return meter({
    label,
    stability,
    learned,
    value: learned ? `${stability}d` : `${stability}/${goal.stability}d`,
    summary:
      `${label}: memory strength ${stability} of ${goal.stability} days, ` +
      `${days} of ${goal.days} successful days${recallNote(recall)}`,
    extra: h(
      "span",
      { class: "strength-days", "aria-hidden": "true" },
      Array.from({ length: goal.days }, (_, index) =>
        h("i", { class: index < days ? "on" : null }),
      ),
    ),
  });
}

function sentenceStrength(card) {
  if (!card.reviews) return null;
  const stability = card.stability ?? 0;
  return meter({
    label: "Sentence",
    stability,
    learned: stability >= state.learnedAt.stability,
    value: `${stability}d`,
    summary:
      `This sentence card: memory strength ${stability} days, ` +
      `reviewed ${plural(card.reviews, "time")}${recallNote(card.recall)}`,
    extra: h(
      "span",
      { class: "strength-reviews", "aria-hidden": "true" },
      plural(card.reviews, "review"),
    ),
  });
}

function reviewsNote(card) {
  return h(
    "span",
    {
      class: "card-reviews",
      title:
        card.stability === null
          ? null
          : `This card's memory lasts about ${card.stability} days`,
    },
    card.reviews ? `Reviewed ${plural(card.reviews, "time")}` : "First review",
  );
}

function prefetchStrokes(cards) {
  for (const card of cards) {
    strokes.prefetch(card.text);
    for (const word of card.words || []) strokes.prefetch(word.word);
  }
}

function senseItems(senses) {
  return senses.map((sense) =>
    h(
      "li",
      {},
      h(
        "p",
        { class: sense.usage ? "usage" : null },
        sense.notes.map((note) => h("span", { class: "pos" }, note)),
        sense.text,
      ),
    ),
  );
}

function readingList(readings, { limit = Infinity } = {}) {
  if (!readings.length) return [h("p", { class: "muted" }, "No definition")];
  return readings.map((reading) =>
    h(
      "section",
      { class: "reading-group" },
      readings.length > 1
        ? h("h4", { class: "reading-pinyin" }, reading.pinyin)
        : null,
      h(
        "ol",
        { class: `senses${reading.senses.length === 1 ? " single" : ""}` },
        senseItems(reading.senses.slice(0, limit)),
      ),
      reading.classifiers.length && limit === Infinity
        ? h(
            "p",
            { class: "classifiers muted small" },
            `Measure word${reading.classifiers.length > 1 ? "s" : ""}: `,
            h("span", { lang: "zh-Hans" }, reading.classifiers.join(", ")),
          )
        : null,
    ),
  );
}

function promptList(prompts) {
  return h(
    "ol",
    { class: `senses${prompts.length === 1 ? " single" : ""}` },
    prompts.map((text) => h("li", {}, h("p", {}, text))),
  );
}

function isFlagged(word) {
  return state.flags.get(word) ?? false;
}

function flagToggle(word) {
  return h(
    "button",
    {
      type: "button",
      class: "icon-button small",
      "aria-pressed": String(isFlagged(word)),
      title: `Flag ${word} for correction`,
      dataset: { flagWord: word },
    },
    icon("flag"),
  );
}

function sayButton() {
  return h(
    "button",
    {
      type: "button",
      class: "say",
      title: "Replay pronunciation (R)",
      dataset: { play: "" },
    },
    icon("audio"),
    h("span", { class: "sr-only" }, "Replay pronunciation"),
  );
}

function missToggle(word) {
  return h(
    "button",
    {
      type: "button",
      class: "miss-toggle",
      "aria-pressed": String(state.marked.has(word)),
      title: `Mark ${word} as a word you missed`,
      dataset: { missWord: word },
    },
    "Missed",
  );
}

function sentenceEditor(card) {
  const field = (name, label, value, attributes = {}) =>
    h(
      "label",
      {},
      label,
      h("textarea", { name, rows: 2, ...attributes }, value),
    );
  const status = h("p", { class: "muted small", role: "status" });
  return h(
    "form",
    {
      class: "sentence-editor",
      onsubmit: (event) => {
        event.preventDefault();
        saveSentence(event.target, status);
      },
    },
    field("pinyin", "Pinyin", card.sentence.pinyin || "", { maxlength: 1000 }),
    field("translation", "Translation", card.translation, {
      maxlength: 2000,
      required: true,
    }),
    h(
      "div",
      { class: "form-actions" },
      status,
      h(
        "button",
        { type: "button", class: "button small", dataset: { editCancel: "" } },
        "Cancel",
      ),
      h("button", { type: "submit", class: "button primary small" }, "Save"),
    ),
  );
}

function renderCard(card) {
  const sentence = card.kind === "sentence";
  const writing = card.direction === "production";
  const face = h("div", { class: "face" });
  const headword = h(
    "div",
    {
      class: `headword${sentence ? " sentence" : ""}${sentence && card.text.length > 16 ? " long" : ""}`,
    },
    hanzi(card.reading),
    sayButton(),
  );
  if (sentence)
    headword.style.setProperty(
      "--length",
      [...card.text.replace(/^[\p{P}\s]+|[\p{P}\s]+$/gu, "")].length,
    );
  face.append(headword);
  if (writing) {
    face.append(
      sentence
        ? h("p", { class: "prompt prompt-translation" }, card.translation)
        : h("div", { class: "prompt" }, promptList(card.prompt)),
    );
  }

  const answer = h("div", { class: "answer" });
  if (sentence) {
    answer.append(
      h(
        "div",
        { class: "sentence-head" },
        h("p", { class: "translation" }, card.translation),
        sentenceStrength(card),
      ),
    );
    if (card.words.length) {
      answer.append(
        h(
          "ul",
          { class: "word-notes" },
          card.words.map((word) =>
            h(
              "li",
              {},
              h(
                "div",
                { class: "word-note-head" },
                h("span", { class: "word-note-word" }, hanzi(word.reading)),
                strength(word),
                h(
                  "span",
                  { class: "word-note-tools" },
                  missToggle(word.word),
                  flagToggle(word.word),
                  h(
                    "a",
                    {
                      class: "icon-button small",
                      href: `/library/words/${encodeURIComponent(word.word)}`,
                      title: `Open ${word.word} in the library`,
                    },
                    icon("library"),
                  ),
                ),
              ),
              readingList(word.readings, { limit: 3 }),
            ),
          ),
        ),
      );
    }
    answer.append(
      h(
        "p",
        { class: "sentence-tools" },
        h(
          "button",
          { type: "button", class: "link", dataset: { edit: "" } },
          "Edit pinyin or translation",
        ),
        h(
          "button",
          { type: "button", class: "link danger", dataset: { skip: "" } },
          "Skip sentence",
        ),
      ),
    );
  } else {
    answer.append(
      ...readingList(card.readings),
      h("div", { class: "card-footer" }, strength(card), reviewsNote(card)),
    );
  }
  face.append(answer);

  const article = h(
    "article",
    {
      class: `card direction-${card.direction} kind-${card.kind}${writing ? " has-pad" : ""}`,
      "data-strokes-locked": true,
    },
    face,
  );
  if (writing) article.append(h("div", { class: "pad", dataset: { pad: "" } }));
  return article;
}

function renderActions() {
  if (!state.current) {
    dom.actions.replaceChildren();
    return;
  }
  if (!state.revealed) {
    dom.actions.replaceChildren(
      h(
        "button",
        {
          type: "button",
          class: "button primary reveal",
          dataset: { reveal: "" },
        },
        "Show answer",
        h("kbd", {}, "Space"),
      ),
    );
    return;
  }
  dom.actions.replaceChildren(
    h(
      "div",
      { class: "ratings" },
      ratings.map(([value, label]) =>
        h(
          "button",
          {
            type: "button",
            class: `rating rating-${value}`,
            dataset: { rating: value },
          },
          h(
            "span",
            { class: "rating-label" },
            h("kbd", {}, String(value)),
            label,
          ),
          h(
            "span",
            { class: "rating-interval" },
            state.current.intervals[value],
          ),
        ),
      ),
    ),
  );
}

function updateChrome() {
  if (!dom) return;
  const remaining = Math.max(
    state.queue.length + (state.current ? 1 : 0),
    state.count - state.ratedSinceFetch,
  );
  const fresh = Math.min(state.fresh, remaining);
  const reviews = remaining - fresh;
  dom.reviews.textContent = String(reviews);
  dom.reviewsLabel.textContent = reviews === 1 ? "review" : "reviews";
  dom.fresh.textContent = String(fresh);
  dom.undo.disabled =
    !state.lastReview || state.outbox.length > 0 || Boolean(state.failure);
  dom.flag.disabled = !state.current?.word;
  if (state.current?.word) {
    const flagged = isFlagged(state.current.word);
    const visible = state.revealed || state.current.direction === "recognition";
    dom.flag.setAttribute("aria-pressed", String(flagged));
    dom.flag.title = `${flagged ? "Unflag" : "Flag"} ${visible ? state.current.word : "this word"}`;
  }
  dom.audio.setAttribute("aria-pressed", String(!state.autoplay));
  dom.audio.title = state.autoplay ? "Turn off autoplay" : "Turn on autoplay";
  dom.audio.querySelector('[data-when="on"]').hidden = !state.autoplay;
  dom.audio.querySelector('[data-when="off"]').hidden = state.autoplay;
  for (const button of document.querySelectorAll("[data-flag-word]"))
    button.setAttribute(
      "aria-pressed",
      String(isFlagged(button.dataset.flagWord)),
    );
}

function clearCard() {
  stopAudio();
  strokes.stop();
  pad?.destroy();
  pad = null;
}

function show(card, { revealed = false } = {}) {
  clearCard();
  state.current = card;
  state.revealed = false;
  state.marked = new Set();
  if (!card) {
    renderDone();
    updateChrome();
    return;
  }
  if (!state.flags.has(card.word)) state.flags.set(card.word, card.flagged);
  for (const word of card.words || [])
    if (!state.flags.has(word.word)) state.flags.set(word.word, word.flagged);
  const element = renderCard(card);
  dom.stage.replaceChildren(element);
  strokes.render(element);
  const slot = element.querySelector("[data-pad]");
  if (slot) pad = new Pad(slot);
  loadAudio(card);
  if (revealed) reveal({ silent: true });
  else {
    renderActions();
    updateChrome();
  }
  prefetch();
}

function reveal({ silent = false } = {}) {
  if (!state.current || state.revealed) return;
  state.revealed = true;
  const article = dom.stage.querySelector(".card");
  article?.classList.add("revealed");
  article?.removeAttribute("data-strokes-locked");
  dom.stage.querySelector(".face")?.scrollTo({ top: 0 });
  renderActions();
  updateChrome();
  if (!silent && state.autoplay) playAudio();
}

function renderDone() {
  const { reviewed, ratings: counts } = state.session;
  const summary = ratings
    .filter(([value]) => counts[value])
    .map(([value, label]) => `${counts[value]} ${label.toLowerCase()}`)
    .join(" · ");
  dom.stage.replaceChildren(
    h(
      "section",
      { class: "empty done" },
      h("p", { class: "empty-glyph", lang: "zh-Hans" }, reviewed ? "好" : "空"),
      h("h1", {}, reviewed ? "Session complete" : "Nothing due right now"),
      h(
        "p",
        { class: "muted" },
        reviewed
          ? `${plural(reviewed, "card")} reviewed · ${summary}`
          : "Come back later, or import more words.",
      ),
      h(
        "div",
        { class: "form-actions center" },
        h("a", { class: "button", href: "/library" }, "Library"),
        h("a", { class: "button primary", href: "/" }, "Done"),
      ),
    ),
  );
  dom.actions.replaceChildren();
}

function renderLoading() {
  clearCard();
  dom.stage.replaceChildren(
    h(
      "div",
      { class: "loading", "aria-busy": "true" },
      h("span", { class: "spinner" }),
    ),
  );
  dom.actions.replaceChildren();
}

function nextCard() {
  const index = state.queue.findIndex((card) => !state.consumed.has(key(card)));
  if (index === -1) {
    state.queue = [];
    return null;
  }
  return state.queue.splice(0, index + 1).at(-1);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-CSRF-Token": csrf(),
      Accept: "application/json",
      ...(typeof options.body === "string"
        ? { "Content-Type": "application/json" }
        : {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      data.error || `Request failed (${response.status}).`,
    );
    error.status = response.status;
    throw error;
  }
  return data;
}

function persistOutbox() {
  try {
    sessionStorage.setItem(outboxKey, JSON.stringify(state.outbox));
  } catch {}
}

function fail(error, retry) {
  if (!dom) return;
  state.failure = { retry };
  const pending = state.outbox.length;
  dom.alert.querySelector("[data-alert-text]").textContent = [
    error instanceof TypeError ? "Connection lost." : error.message,
    pending ? `${plural(pending, "rating")} not saved yet.` : "",
  ]
    .join(" ")
    .trim();
  dom.alert.hidden = false;
  updateChrome();
}

function dismiss() {
  state.failure = null;
  if (dom) dom.alert.hidden = true;
  updateChrome();
}

function flush() {
  if (state.saving) return state.saving;
  const request = (async () => {
    await Promise.resolve();
    try {
      while (state.outbox.length) {
        const item = state.outbox[0];
        try {
          const data = await api("/api/reviews", {
            method: "POST",
            body: JSON.stringify({
              card: item.card,
              version: item.version,
              rating: item.rating,
              marked: item.marked || [],
            }),
          });
          state.lastReview = data.review;
        } catch (error) {
          if (error.status !== 409) throw error;
        }
        state.outbox.shift();
        persistOutbox();
        updateChrome();
      }
      return true;
    } catch (error) {
      fail(error, refill);
      return false;
    } finally {
      if (state.saving === request) state.saving = null;
    }
  })();
  state.saving = request;
  return request;
}

function fetchBatch() {
  if (state.fetching) return state.fetching;
  const generation = state.generation;
  const request = (async () => {
    try {
      const batch = await api("/api/study");
      if (generation !== state.generation) return false;
      state.count = batch.count;
      state.fresh = batch.counts.new;
      state.ratedSinceFetch = 0;
      state.lastReview = batch.lastReview;
      state.learnedAt = batch.learnedAt;
      state.queue = batch.cards.filter(
        (card) =>
          card.id !== state.current?.id && !state.consumed.has(key(card)),
      );
      prefetchStrokes(state.queue);
      return true;
    } catch (error) {
      if (generation === state.generation) fail(error, refill);
      return false;
    } finally {
      if (state.fetching === request) state.fetching = null;
      updateChrome();
    }
  })();
  state.fetching = request;
  return request;
}

async function refill() {
  const generation = state.generation;
  dismiss();
  if (!(await flush()) || !(await fetchBatch())) return;
  if (generation === state.generation && !state.current) show(nextCard());
}

function prefetch() {
  if (state.queue.length > 3 || state.failure) return;
  flush().then((saved) => {
    if (saved && state.queue.length <= 3) fetchBatch();
  });
}

function rate(value) {
  const card = state.current;
  if (!card || !state.revealed || state.failure) return;
  state.outbox.push({
    card: card.id,
    version: card.version,
    rating: value,
    marked: [...state.marked],
  });
  persistOutbox();
  state.consumed.add(key(card));
  state.ratedSinceFetch += 1;
  state.session.reviewed += 1;
  state.session.ratings[value] += 1;
  if (card.new) state.fresh = Math.max(0, state.fresh - 1);
  const next = nextCard();
  if (next) {
    show(next);
    flush();
    return;
  }
  state.current = null;
  renderLoading();
  updateChrome();
  refill();
}

async function undo() {
  if (!state.lastReview || state.outbox.length || state.failure) return;
  dom.undo.disabled = true;
  const generation = state.generation;
  try {
    await api("/api/reviews/undo", {
      method: "POST",
      body: JSON.stringify({ review: state.lastReview }),
    });
  } catch (error) {
    fail(error, dismiss);
    return;
  }
  if (generation !== state.generation) return;
  state.consumed.clear();
  state.current = null;
  state.session.reviewed = Math.max(0, state.session.reviewed - 1);
  renderLoading();
  if ((await fetchBatch()) && generation === state.generation) show(nextCard());
}

function advance() {
  const next = nextCard();
  if (next) show(next);
  else {
    state.current = null;
    renderLoading();
    refill();
  }
}

async function skipSentence() {
  const card = state.current;
  if (!card?.sentence) return;
  try {
    await api(`/api/sentences/${card.sentence.id}/skip`, {
      method: "POST",
      body: JSON.stringify({ version: card.sentence.version }),
    });
  } catch (error) {
    fail(error, dismiss);
    return;
  }
  if (state.current !== card) return;
  const removed = [
    card,
    ...state.queue.filter((other) => other.family === card.family),
  ];
  state.queue = state.queue.filter((other) => other.family !== card.family);
  state.consumed.add(key(card));
  state.count = Math.max(0, state.count - removed.length);
  state.fresh = Math.max(
    0,
    state.fresh - removed.filter((other) => other.new).length,
  );
  advance();
}

function editSentence() {
  const tools = dom.stage.querySelector(".sentence-tools");
  if (!tools || !state.current?.sentence) return;
  const editor = sentenceEditor(state.current);
  tools.replaceWith(editor);
  editor.querySelector("textarea").focus();
}

async function saveSentence(form, status) {
  const card = state.current;
  if (!card?.sentence) return;
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  status.textContent = "Saving…";
  try {
    const data = await api(`/api/sentences/${card.sentence.id}`, {
      method: "POST",
      body: JSON.stringify({
        card: card.id,
        version: card.sentence.version,
        pinyin: form.elements.pinyin.value,
        translation: form.elements.translation.value,
      }),
    });
    if (state.current !== card) return;
    state.queue = state.queue.filter((other) => other.family !== card.family);
    if (data.card) show(data.card, { revealed: true });
    else {
      state.count = Math.max(0, state.count - 1);
      if (card.new) state.fresh = Math.max(0, state.fresh - 1);
      advance();
    }
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
  }
}

function toggleMissed(button) {
  const word = button.dataset.missWord;
  if (state.marked.has(word)) state.marked.delete(word);
  else state.marked.add(word);
  button.setAttribute("aria-pressed", String(state.marked.has(word)));
}

async function toggleFlag(word) {
  const flagged = !isFlagged(word);
  state.flags.set(word, flagged);
  updateChrome();
  try {
    await api("/flags/words", {
      method: "POST",
      body: new URLSearchParams({ word, flagged: flagged ? "1" : "0" }),
    });
  } catch (error) {
    state.flags.set(word, !flagged);
    fail(error, dismiss);
  }
}

function loadAudio(card) {
  player = new Audio(card.audio);
  player.preload = state.autoplay ? "auto" : "none";
  const playing = (value) =>
    dom?.stage.querySelector("[data-play]")?.classList.toggle("playing", value);
  player.addEventListener("playing", () => playing(true));
  for (const type of ["ended", "pause", "error"])
    player.addEventListener(type, () => playing(false));
}

function playAudio() {
  if (!state.current) return;
  if (!player || player.error) loadAudio(state.current);
  player.currentTime = 0;
  player.play().catch(() => {});
}

function stopAudio() {
  if (!player) return;
  player.pause();
  player.removeAttribute("src");
  player.load();
  player = null;
}

function toggleAutoplay() {
  state.autoplay = !state.autoplay;
  document.cookie = `yiju-audio=${state.autoplay ? "on" : "off"}; path=/; max-age=31536000; samesite=lax`;
  if (!state.autoplay) player?.pause();
  updateChrome();
}

function drawStrokes(context, lines, size, lineWidth, color) {
  context.clearRect(0, 0, size, size);
  context.strokeStyle = color;
  context.lineWidth = lineWidth;
  context.lineCap = "round";
  context.lineJoin = "round";
  for (const stroke of lines) {
    context.beginPath();
    context.moveTo(stroke[0][0] * size, stroke[0][1] * size);
    for (const [x, y] of stroke.slice(1)) context.lineTo(x * size, y * size);
    if (stroke.length === 1)
      context.lineTo(stroke[0][0] * size + 0.1, stroke[0][1] * size);
    context.stroke();
  }
}

class Pad {
  constructor(slot) {
    this.drawings = [[]];
    this.index = 0;
    this.pointer = null;
    this.canvas = h("canvas", {
      class: "pad-canvas",
      "aria-label": "Handwriting pad",
    });
    this.thumbs = h("div", {
      class: "pad-thumbs",
      role: "group",
      "aria-label": "Characters",
    });
    this.overview = h("div", { class: "pad-overview", hidden: true });
    this.undoButton = this.tool("undo", "Undo stroke (Z)", () => this.undo());
    this.clearButton = this.tool("erase", "Clear", () => this.clear());
    this.nextButton = this.tool("plus", "Next character", () => this.advance());
    this.nextButton.classList.add("pad-next");
    slot.append(
      h("div", { class: "pad-surface" }, this.canvas, this.overview),
      h(
        "div",
        { class: "pad-tools" },
        this.undoButton,
        this.clearButton,
        this.thumbs,
        this.nextButton,
      ),
    );
    this.width = 0;
    this.observer = new ResizeObserver(() => {
      this.paint();
      if (this.thumbs.clientWidth !== this.width) this.sync();
    });
    this.observer.observe(this.canvas);
    this.observer.observe(this.thumbs);
    this.canvas.addEventListener("pointerdown", (event) => this.down(event));
    this.canvas.addEventListener("pointermove", (event) => this.move(event));
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"])
      this.canvas.addEventListener(type, (event) => this.up(event));
    this.sync();
  }

  tool(name, label, handler) {
    return h(
      "button",
      {
        type: "button",
        class: "icon-button",
        title: label,
        "aria-label": label,
        onclick: handler,
      },
      icon(name),
    );
  }

  get strokes() {
    return this.drawings[this.index];
  }

  point(event) {
    const rect = this.canvas.getBoundingClientRect();
    return [
      Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    ];
  }

  down(event) {
    if (this.pointer !== null || event.button !== 0) return;
    event.preventDefault();
    this.closeOverview();
    this.pointer = event.pointerId;
    this.canvas.setPointerCapture(event.pointerId);
    this.strokes.push([this.point(event)]);
    this.paint();
  }

  move(event) {
    if (event.pointerId !== this.pointer) return;
    event.preventDefault();
    const stroke = this.strokes.at(-1);
    for (const sample of event.getCoalescedEvents?.() || [event])
      stroke.push(this.point(sample));
    this.paint();
  }

  up(event) {
    if (event.pointerId !== this.pointer) return;
    this.pointer = null;
    if (this.canvas.hasPointerCapture(event.pointerId))
      this.canvas.releasePointerCapture(event.pointerId);
    this.sync();
  }

  undo() {
    this.strokes.pop();
    this.paint();
    this.sync();
  }

  clear() {
    this.drawings[this.index] = [];
    this.paint();
    this.sync();
  }

  advance() {
    if (!this.strokes.length) return;
    const empty = this.drawings.findIndex(
      (drawing, index) => index > this.index && !drawing.length,
    );
    if (empty === -1) {
      this.drawings.push([]);
      this.index = this.drawings.length - 1;
    } else this.index = empty;
    this.paint();
    this.sync();
  }

  select(index) {
    this.closeOverview();
    if (index === this.index) return;
    if (!this.strokes.length) {
      this.drawings.splice(this.index, 1);
      if (index > this.index) index -= 1;
    }
    this.index = index;
    this.paint();
    this.sync();
  }

  paint() {
    const size = this.canvas.getBoundingClientRect().width;
    if (!size) return;
    const ratio = window.devicePixelRatio || 1;
    this.canvas.width = this.canvas.height = Math.round(size * ratio);
    const context = this.canvas.getContext("2d");
    context.scale(ratio, ratio);
    drawStrokes(
      context,
      this.strokes,
      size,
      Math.max(3, size / 64),
      getComputedStyle(this.canvas).color,
    );
  }

  thumb(index, color) {
    const canvas = h("canvas", { width: 96, height: 96 });
    drawStrokes(canvas.getContext("2d"), this.drawings[index], 96, 3, color);
    return h(
      "button",
      {
        type: "button",
        class: "pad-thumb",
        "aria-pressed": String(index === this.index),
        "aria-label": `Character ${index + 1}`,
        onclick: () => this.select(index),
      },
      canvas,
    );
  }

  visible() {
    return this.drawings
      .map((_, index) => index)
      .filter((index) => this.drawings[index].length || index === this.index);
  }

  sync() {
    const empty = !this.strokes.length;
    this.undoButton.disabled = empty;
    this.clearButton.disabled = empty;
    this.nextButton.disabled = empty;
    this.width = this.thumbs.clientWidth;
    const indices = this.visible();
    if (indices.length < 2) {
      this.thumbs.replaceChildren();
      this.closeOverview();
      return;
    }
    const color = getComputedStyle(this.canvas).color;
    const size = Number.parseFloat(
      getComputedStyle(this.thumbs).getPropertyValue("--thumb-size"),
    );
    const capacity = Math.max(
      2,
      Math.floor((this.width + thumbGap) / (size + thumbGap)),
    );
    let shown = indices;
    let hidden = 0;
    if (indices.length > capacity) {
      const position = indices.indexOf(this.index);
      const end = Math.min(
        indices.length,
        Math.max(position + 1, capacity - 1),
      );
      shown = indices.slice(end - (capacity - 1), end);
      hidden = indices.length - shown.length;
    }
    const nodes = shown.map((index) => this.thumb(index, color));
    if (hidden) {
      nodes.unshift(
        h(
          "button",
          {
            type: "button",
            class: "pad-more",
            title: "Show all characters",
            "aria-expanded": String(!this.overview.hidden),
            onclick: () => this.toggleOverview(),
          },
          `+${hidden}`,
        ),
      );
    } else this.closeOverview();
    this.thumbs.replaceChildren(...nodes);
    if (!this.overview.hidden) this.renderOverview();
  }

  renderOverview() {
    const color = getComputedStyle(this.canvas).color;
    this.overview.replaceChildren(
      ...this.visible().map((index) => this.thumb(index, color)),
    );
  }

  toggleOverview() {
    if (this.overview.hidden) {
      this.renderOverview();
      this.overview.hidden = false;
    } else this.overview.hidden = true;
    this.thumbs
      .querySelector(".pad-more")
      ?.setAttribute("aria-expanded", String(!this.overview.hidden));
  }

  closeOverview() {
    if (this.overview.hidden) return;
    this.overview.hidden = true;
    this.thumbs
      .querySelector(".pad-more")
      ?.setAttribute("aria-expanded", "false");
  }

  destroy() {
    this.observer.disconnect();
  }
}

function onClick(event) {
  if (pad && !event.target.closest(".pad-overview, .pad-more"))
    pad.closeOverview();
  const target = event.target.closest("button");
  if (!dom || !target || !dom.root.contains(target)) return;
  if (target.matches("[data-reveal]")) reveal();
  else if (target.matches("[data-rating]")) rate(Number(target.dataset.rating));
  else if (target.matches("[data-play]")) playAudio();
  else if (target.matches("[data-skip]")) skipSentence();
  else if (target.matches("[data-edit]")) editSentence();
  else if (target.matches("[data-edit-cancel]"))
    show(state.current, { revealed: true });
  else if (target.matches("[data-miss-word]")) toggleMissed(target);
  else if (target.matches("[data-flag-word]"))
    toggleFlag(target.dataset.flagWord);
  else if (target === dom.flag && state.current) toggleFlag(state.current.word);
  else if (target === dom.undo) undo();
  else if (target === dom.audio) toggleAutoplay();
  else if (target.matches("[data-retry]")) {
    const retry = state.failure?.retry;
    dismiss();
    retry?.();
  }
}

function onKey(event) {
  if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
  if (event.target.closest("input, textarea, select, [contenteditable]"))
    return;
  if (event.key === "Escape") pad?.closeOverview();
  else if (event.code === "Space" && state.current && !state.revealed) {
    event.preventDefault();
    reveal();
  } else if (/^[1-4]$/.test(event.key) && state.revealed) {
    event.preventDefault();
    rate(Number(event.key));
  } else if (event.key === "r" && state.revealed) playAudio();
  else if (event.key === "u") undo();
  else if (event.key === "z" && pad && !state.revealed) pad.undo();
}

export function mount(root) {
  state.generation += 1;
  state.fetching = null;
  dom = {
    root,
    stage: root.querySelector("[data-stage]"),
    actions: root.querySelector("[data-actions]"),
    alert: root.querySelector(".study-alert"),
    reviews: root.querySelector("[data-reviews]"),
    reviewsLabel: root.querySelector("[data-reviews-label]"),
    fresh: root.querySelector("[data-new]"),
    undo: root.querySelector("[data-undo]"),
    flag: root.querySelector("[data-flag-card]"),
    audio: root.querySelector("[data-audio-toggle]"),
  };
  listeners = new AbortController();
  document.addEventListener("click", onClick, { signal: listeners.signal });
  document.addEventListener("keydown", onKey, { signal: listeners.signal });
  const batch = JSON.parse(root.querySelector("#study-data").textContent);
  state.count = batch.count;
  state.fresh = batch.counts.new;
  state.lastReview = batch.lastReview;
  state.learnedAt = batch.learnedAt;
  state.ratedSinceFetch = 0;
  state.failure = null;
  state.current = null;
  state.consumed = new Set(
    state.outbox.map((item) => `${item.card}:${item.version}`),
  );
  state.queue = batch.cards;
  prefetchStrokes(state.queue);
  if (state.outbox.length) {
    renderLoading();
    refill();
    return;
  }
  show(nextCard());
}

export function unmount() {
  clearCard();
  listeners?.abort();
  listeners = null;
  dom = null;
  state.current = null;
  state.generation += 1;
}

export function leave() {
  if (!state.outbox.length) return Promise.resolve();
  return Promise.race([
    flush(),
    new Promise((resolve) => setTimeout(resolve, 1500)),
  ]);
}
