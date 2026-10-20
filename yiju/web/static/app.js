import { enhance, prefetch } from "./strokes.js";

const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const units = [
  ["year", 31536000],
  ["month", 2592000],
  ["week", 604800],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];
const native = /^\/(static|audio|strokes|backup|flags\/words\/export|manifest)/;

const scrollKey = "yiju-scroll";
const positions = new Map();
let page = null;
let navigating = false;
let visiting = 0;
let installPrompt = null;
let speaker = null;

function csrf() {
  return document.querySelector('meta[name="csrf"]')?.content || "";
}

function formatTimes(root = document) {
  for (const element of root.querySelectorAll("time[data-relative]")) {
    const date = new Date(element.dateTime);
    if (Number.isNaN(date.getTime())) continue;
    const seconds = (date - Date.now()) / 1000;
    const [unit, size] = units.find(
      ([, size]) => Math.abs(seconds) >= size,
    ) || ["minute", 60];
    element.textContent = relative.format(Math.round(seconds / size), unit);
    element.title = date.toLocaleString();
  }
  for (const element of root.querySelectorAll("time[data-local]")) {
    const date = new Date(element.dateTime);
    if (!Number.isNaN(date.getTime()))
      element.textContent = date.toLocaleString();
  }
}

async function errorMessage(response) {
  const body = await response.json().catch(() => ({}));
  return body.error || `Something went wrong (${response.status}).`;
}

async function post(url, data) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "X-CSRF-Token": csrf(), Accept: "application/json" },
    body: new URLSearchParams(data),
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json();
}

async function fragment(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "X-CSRF-Token": csrf(), "X-Fragment": "1" },
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  const template = document.createElement("template");
  template.innerHTML = await response.text();
  return template.content.firstElementChild;
}

function toast(message) {
  let container = document.querySelector(".toasts");
  if (!container) {
    container = document.createElement("div");
    container.className = "toasts";
    container.setAttribute("role", "status");
    document.querySelector("#main").prepend(container);
  }
  const item = document.createElement("p");
  item.className = "toast";
  item.textContent = message;
  container.append(item);
  setTimeout(() => item.remove(), 4000);
}

const progress = {
  bar: null,
  timer: null,
  start() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      if (!this.bar) {
        this.bar = document.createElement("div");
        this.bar.className = "nav-progress";
        document.documentElement.append(this.bar);
      }
      this.bar.classList.remove("done");
      this.bar.classList.add("active");
    }, 120);
  },
  done() {
    clearTimeout(this.timer);
    if (!this.bar?.classList.contains("active")) return;
    this.bar.classList.add("done");
    this.bar.classList.remove("active");
  },
};

async function mountPage() {
  formatTimes();
  enhance(document);
  for (const button of document.querySelectorAll("[data-back]"))
    button.hidden = history.length < 2;
  for (const item of document.querySelectorAll(".toast"))
    setTimeout(() => item.remove(), 4000);
  const install = document.querySelector("[data-install]");
  if (install) install.hidden = !installPrompt;
  const stage = document.querySelector("[data-stage]");
  if (stage) {
    const root = document.body;
    const module = await import(stage.dataset.module);
    if (root.contains(stage)) {
      module.mount(root);
      page = module;
    }
  }
}

function unmountPage() {
  page?.unmount();
  page = null;
}

function render(doc) {
  unmountPage();
  document.title = doc.title;
  document.body.className = doc.body.className;
  document.body.replaceChildren(...doc.body.childNodes);
  mountPage();
}

function entry() {
  return history.state?.key;
}

function newEntry() {
  return { key: crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}` };
}

function remember() {
  if (!navigating && entry()) positions.set(entry(), scrollY);
}

function persistPositions() {
  try {
    sessionStorage.setItem(
      scrollKey,
      JSON.stringify([...positions].slice(-100)),
    );
  } catch {}
}

function restorePositions() {
  try {
    for (const [key, value] of JSON.parse(
      sessionStorage.getItem(scrollKey) || "[]",
    ))
      positions.set(key, value);
  } catch {}
}

async function refreshCounts() {
  const chips = document.querySelector(".chips");
  if (!chips) return;
  try {
    const response = await fetch(location.href);
    const next = new DOMParser()
      .parseFromString(await response.text(), "text/html")
      .querySelector(".chips");
    if (next && chips.isConnected) chips.replaceWith(next);
  } catch {}
}

async function visit(
  url,
  { method = "GET", body = null, push = true, scroll = 0 } = {},
) {
  const ticket = ++visiting;
  if (push) remember();
  navigating = true;
  progress.start();
  try {
    await page?.leave();
    const response = await fetch(url, {
      method,
      body,
      headers: method === "GET" ? {} : { "X-CSRF-Token": csrf() },
    });
    if (ticket !== visiting) return;
    if (!(response.headers.get("Content-Type") || "").includes("text/html")) {
      location.assign(response.url);
      return;
    }
    const doc = new DOMParser().parseFromString(
      await response.text(),
      "text/html",
    );
    if (ticket !== visiting) return;
    const destination = new URL(response.url);
    if (push) {
      if (destination.href === location.href)
        history.replaceState(history.state || newEntry(), "", destination);
      else history.pushState(newEntry(), "", destination);
    }
    render(doc);
    const target =
      destination.hash &&
      document.getElementById(decodeURIComponent(destination.hash.slice(1)));
    if (target) target.scrollIntoView();
    else scrollTo(0, scroll);
    document.querySelector("#main")?.focus({ preventScroll: true });
  } catch {
    if (ticket === visiting) location.assign(url);
  } finally {
    if (ticket === visiting) {
      navigating = false;
      remember();
      progress.done();
    }
  }
}

function routable(url) {
  return url.origin === location.origin && !native.test(url.pathname);
}

async function toggleItem(item) {
  const row = item.querySelector(".row");
  const list = item.closest(".list");
  const top = row.getBoundingClientRect().top;
  const opening = !item.classList.contains("open");
  for (const other of list.querySelectorAll(".item.open")) {
    if (other === item) continue;
    other.classList.remove("open");
    other.querySelector(".row").setAttribute("aria-expanded", "false");
  }
  let current = item;
  if (opening && !item.querySelector(".details")) {
    item.classList.add("loading");
    prefetch(row.querySelector(".row-hanzi, .row-sentence")?.textContent || "");
    try {
      current = await fragment(item.dataset.url);
      item.replaceWith(current);
      formatTimes(current);
      enhance(current);
    } catch (error) {
      item.classList.remove("loading");
      toast(error.message);
      return;
    }
  }
  current.classList.toggle("open", opening);
  current.querySelector(".row").setAttribute("aria-expanded", String(opening));
  const shift = current.querySelector(".row").getBoundingClientRect().top - top;
  if (shift) scrollBy(0, shift);
  const url = new URL(location.href);
  if (opening) url.searchParams.set("open", current.dataset.item);
  else url.searchParams.delete("open");
  history.replaceState(history.state, "", url);
}

async function submitItem(form, submitter) {
  const item = form.closest(".item");
  const data = new FormData(form, submitter);
  for (const button of form.querySelectorAll("button")) button.disabled = true;
  try {
    const next = await fragment(form.action, {
      method: "POST",
      body: new URLSearchParams(data),
    });
    next.classList.add("open");
    item.replaceWith(next);
    formatTimes(next);
    enhance(next);
    refreshCounts();
  } catch (error) {
    toast(error.message);
    for (const button of form.querySelectorAll("button"))
      button.disabled = false;
  }
}

function say(url) {
  speaker?.pause();
  speaker = new Audio(url);
  speaker.play().catch(() => toast("Pronunciation is unavailable right now."));
}

async function toggleFlag(button) {
  const word = button.dataset.flag;
  const flagged = button.getAttribute("aria-pressed") !== "true";
  button.disabled = true;
  try {
    await post("/flags/words", { word, flagged: flagged ? "1" : "0" });
    button.setAttribute("aria-pressed", String(flagged));
    const item = button.closest(".item");
    const marker = item?.querySelector(".row .flagged");
    if (marker) marker.hidden = !flagged;
    if (item) refreshCounts();
    const note = (item || document).querySelector(
      `[data-flag-note="${CSS.escape(word)}"]`,
    );
    if (note) {
      note.hidden = !flagged;
      if (flagged) note.querySelector("textarea").focus();
    }
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function saveNote(form) {
  const status = form.querySelector("[data-note-status]");
  status.textContent = "Saving…";
  try {
    await post("/flags/words", {
      word: form.dataset.flagNote,
      flagged: "1",
      note: form.elements.note.value,
    });
    status.textContent = "Saved";
  } catch (error) {
    status.textContent = error.message;
  }
}

const search = {
  timer: null,
  controller: null,
  async update(form) {
    const url = new URL(form.action || location.href);
    url.search = "";
    for (const [name, value] of new FormData(form)) {
      if (
        value &&
        !(name === "filter" && value === "all") &&
        !(name === "sort" && value === "status")
      )
        url.searchParams.set(name, value);
    }
    this.controller?.abort();
    this.controller = new AbortController();
    try {
      const response = await fetch(url, { signal: this.controller.signal });
      const html = new DOMParser().parseFromString(
        await response.text(),
        "text/html",
      );
      const next = html.querySelector("[data-results]");
      const current = document.querySelector("[data-results]");
      if (!next || !current) return;
      current.replaceWith(next);
      history.replaceState(history.state, "", url);
      formatTimes(next);
    } catch (error) {
      if (error.name !== "AbortError") visit(url.href);
    }
  },
};

document.addEventListener("click", (event) => {
  if (event.defaultPrevented) return;
  const flag = event.target.closest("[data-flag]");
  if (flag) {
    toggleFlag(flag);
    return;
  }
  const speak = event.target.closest("[data-say]");
  if (speak) {
    say(speak.dataset.say);
    return;
  }
  const row = event.target.closest(".item > .row");
  if (row) {
    toggleItem(row.parentElement);
    return;
  }
  if (event.target.closest("[data-back]")) {
    history.back();
    return;
  }
  const link = event.target.closest("a[href]");
  if (
    !link ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey ||
    link.target ||
    link.hasAttribute("download")
  )
    return;
  const url = new URL(link.href);
  if (!routable(url)) return;
  if (
    url.hash &&
    url.pathname === location.pathname &&
    url.search === location.search
  )
    return;
  event.preventDefault();
  visit(url.href);
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (event.defaultPrevented) return;
  if (form.matches("[data-flag-note]")) {
    event.preventDefault();
    saveNote(form);
    return;
  }
  if (form.matches("[data-live-search]")) {
    event.preventDefault();
    search.update(form);
    return;
  }
  if (form.matches("[data-item-form]")) {
    event.preventDefault();
    submitItem(form, event.submitter);
    return;
  }
  const url = new URL(form.action || location.href);
  if (form.target || !routable(url)) return;
  event.preventDefault();
  if (form.matches("[data-busy]")) {
    const button = form.querySelector('[type="submit"]');
    if (button) {
      button.disabled = true;
      button.textContent = form.dataset.busy;
    }
  }
  const data = new FormData(form, event.submitter);
  if (form.method.toLowerCase() === "get") {
    url.search = new URLSearchParams(data).toString();
    visit(url.href);
  } else {
    visit(url.href, {
      method: "POST",
      body:
        form.enctype === "multipart/form-data"
          ? data
          : new URLSearchParams(data),
    });
  }
});

const pinyinEdges = /^([^\p{L}\p{N}_]*)(.*?)([^\p{L}\p{N}_]*)$/u;

function pinyinWords(block) {
  const field = block.closest("form").elements[block.dataset.readings];
  const pieces = field.value.split(/(\s+)/);
  const words = pieces.flatMap((piece, index) =>
    /\p{L}/u.test(piece) ? [index] : [],
  );
  const count = block
    .closest(".check-item")
    .querySelectorAll(".check-sentence .token").length;
  return { field, pieces, words: words.length === count ? words : [] };
}

function syncReadings(block) {
  const { pieces, words } = pinyinWords(block);
  for (const reading of block.querySelectorAll(".reading")) {
    const index = words[Number(reading.dataset.word)];
    const current =
      index === undefined
        ? null
        : pieces[index].match(pinyinEdges)[2].toLowerCase();
    for (const input of reading.querySelectorAll("input")) {
      input.disabled = current === null;
      input.checked = input.value.toLowerCase() === current;
    }
  }
}

function chooseReading(input) {
  const block = input.closest("[data-readings]");
  const { field, pieces, words } = pinyinWords(block);
  const index = words[Number(input.closest(".reading").dataset.word)];
  if (index === undefined) return syncReadings(block);
  const [, before, spoken, after] = pieces[index].match(pinyinEdges);
  const capital = spoken[0] !== spoken[0].toLowerCase();
  const value = capital
    ? input.value[0].toUpperCase() + input.value.slice(1)
    : input.value;
  pieces[index] = before + value + after;
  field.value = pieces.join("");
}

document.addEventListener("input", (event) => {
  const form = event.target.closest("[data-live-search]");
  if (!form || event.target.type !== "search") return;
  clearTimeout(search.timer);
  search.timer = setTimeout(() => search.update(form), 200);
});

document.addEventListener("input", (event) => {
  const field = event.target;
  if (!field.name?.endsWith("-pinyin") || !field.form) return;
  const block = field.form.querySelector(`[data-readings="${field.name}"]`);
  if (block) syncReadings(block);
});

document.addEventListener("change", (event) => {
  if (event.target.matches(".reading-choice input"))
    chooseReading(event.target);
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-auto-submit]"))
    event.target.form.requestSubmit();
});

document.addEventListener("click", async (event) => {
  if (!event.target.closest("[data-install-button]") || !installPrompt) return;
  await installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  const row = document.querySelector("[data-install]");
  if (row) row.hidden = true;
});

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  const row = document.querySelector("[data-install]");
  if (row) row.hidden = false;
});

window.addEventListener("appinstalled", () => {
  installPrompt = null;
  const row = document.querySelector("[data-install]");
  if (row) row.hidden = true;
});

window.addEventListener("popstate", (event) => {
  visit(location.href, {
    push: false,
    scroll: positions.get(event.state?.key) || 0,
  });
});

window.addEventListener("scroll", remember, { passive: true });
window.addEventListener("pagehide", persistPositions);

history.scrollRestoration = "manual";
restorePositions();
if (!entry()) history.replaceState({ ...history.state, ...newEntry() }, "");
const initial = positions.get(entry());
mountPage();
if (initial) scrollTo(0, initial);
