let studyPending = false;
let studyObserver;
let studyUpdating;
let studyDeparting = false;

function layoutStudy() {
  const session = document.querySelector("#study-session");
  if (!session) return;
  if (
    matchMedia(
      "(max-width: 700px), (max-width: 1000px) and (max-height: 500px)",
    ).matches
  ) {
    session.querySelectorAll(".question-text").forEach((text) => {
      text.style.removeProperty("--question-font-size");
    });
    session
      .querySelector(".study-body")
      ?.style.removeProperty("--question-height");
    const viewport = session.querySelector(".answer-pages");
    if (viewport) {
      viewport.scrollLeft = 0;
      viewport.dataset.page = "0";
      viewport.dataset.pages = "1";
      session.querySelector(".answer-pagination").hidden = true;
    }
    return;
  }
  const question = session.querySelector(".question");
  const text = question?.querySelector(".question-text:not([hidden])");
  if (text) {
    const content = question.querySelector(".question-content");
    text.style.removeProperty("--question-font-size");
    const body = question.closest(".study-body");
    if (
      matchMedia("(max-width: 700px)").matches &&
      question.closest(".study-card").classList.contains("revealed")
    ) {
      const height = Math.min(
        Math.max(60, body.clientHeight * 0.6),
        Math.max(60, content.offsetHeight),
      );
      body.style.setProperty("--question-height", `${height}px`);
    } else {
      body.style.removeProperty("--question-height");
    }
    let high = parseFloat(getComputedStyle(text).fontSize);
    let low = 16;
    if (content.offsetHeight > question.clientHeight) {
      for (let step = 0; step < 8; step++) {
        const size = (low + high) / 2;
        text.style.setProperty("--question-font-size", `${size}px`);
        if (content.offsetHeight > question.clientHeight) high = size;
        else low = size;
      }
      text.style.setProperty("--question-font-size", `${low}px`);
    }
  }
  const viewport = session.querySelector(".answer-pages");
  if (!viewport?.clientWidth) return;
  const gap = parseFloat(
    getComputedStyle(viewport.firstElementChild).columnGap,
  );
  const stride = viewport.clientWidth + gap;
  const pages = Math.max(
    1,
    Math.ceil((viewport.scrollWidth + gap - 1) / stride),
  );
  const page = Math.min(Number(viewport.dataset.page || 0), pages - 1);
  viewport.dataset.page = page;
  viewport.dataset.pages = pages;
  viewport.scrollLeft = page * stride;
  session.querySelector(".answer-pagination").hidden = pages === 1;
  session.querySelector("[data-answer-page]").textContent =
    `${page + 1} / ${pages}`;
  session.querySelector("[data-answer-previous]").disabled = page === 0;
  session.querySelector("[data-answer-next]").disabled = page === pages - 1;
}

function initializeStudy() {
  studyObserver?.disconnect();
  studyObserver = new ResizeObserver(() => requestAnimationFrame(layoutStudy));
  document.querySelectorAll(".question, .answer-pages").forEach((element) => {
    studyObserver.observe(element);
  });
  requestAnimationFrame(layoutStudy);
}

document.addEventListener("yiju:drawing-history", () =>
  requestAnimationFrame(layoutStudy),
);

let preparedCards = Array.from(
  document.querySelector("#study-prepared")?.content.children || [],
);
let outbox = [];
let saving;
let preparing;
let lastReview = document.querySelector("[name=review_id]")?.value || "";
let waitingForCard = false;
let studyFailure = false;
let studyRevision = 0;
const consumedCards = new Set();
const outboxKey = "yiju-study-outbox";

function cardKey(session) {
  return `${session.dataset.cardId}:${session.dataset.cardVersion}`;
}

function persistOutbox() {
  sessionStorage.setItem(outboxKey, JSON.stringify(outbox));
}

function syncStudyControls() {
  const session = document.querySelector("#study-session");
  if (!session) return;
  const blocked =
    studyPending || waitingForCard || studyFailure || studyDeparting;
  if (studyPending || waitingForCard) session.setAttribute("aria-busy", "true");
  else session.removeAttribute("aria-busy");
  session
    .querySelectorAll(".rating-buttons button, .sentence-actions button")
    .forEach((button) => {
      button.disabled = blocked;
    });
  const undo = session.querySelector("[data-study-undo]");
  if (undo) {
    undo.hidden = !lastReview;
    undo.querySelector("[name=review_id]").value = lastReview;
    undo.querySelector("button").disabled = blocked || outbox.length > 0;
  }
}

function replaceStudy(next) {
  document.dispatchEvent(new Event("yiju:unmount"));
  document.querySelector("#study-session").replaceWith(next);
  initializeStudy();
  syncStudyControls();
  document.dispatchEvent(
    new CustomEvent("yiju:mounted", { detail: { studyOnly: true } }),
  );
  history.replaceState(history.state, "", "/study");
  next.focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "instant" });
}

function parseStudy(html) {
  return new DOMParser()
    .parseFromString(html, "text/html")
    .querySelector("#study-session");
}

async function studyFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "X-Yiju-Partial": "study", ...options.headers },
  });
  if (!response.ok) {
    const data = response.headers
      .get("Content-Type")
      ?.includes("application/json")
      ? await response.json()
      : {};
    const error = new Error(data.error || "Could not update this session.");
    throw error;
  }
  return response;
}

function showStudyError(error) {
  studyFailure = true;
  const notice = document.querySelector(".study-error");
  notice.querySelector("p").textContent =
    (error instanceof TypeError ? "Connection lost." : error.message) +
    (outbox.length
      ? ` ${outbox.length} ${outbox.length === 1 ? "rating is" : "ratings are"} awaiting confirmation. Retry to save ${outbox.length === 1 ? "it" : "them"}.`
      : " Retry to continue.");
  notice.hidden = false;
  notice.querySelector("[data-study-discard]").hidden = !outbox.length;
  syncStudyControls();
}

async function prepareStudy() {
  if (preparing) return preparing;
  if (outbox.length || studyPending || studyFailure || studyDeparting) return;
  const revision = studyRevision;
  preparing = (async () => {
    try {
      const response = await studyFetch("/study", {
        headers: { "X-Yiju-Buffer": "1" },
      });
      const batch = await response.json();
      if (
        revision !== studyRevision ||
        outbox.length ||
        studyPending ||
        studyDeparting
      )
        return;
      const current = document.querySelector("#study-session");
      preparedCards = batch.cards
        .map(parseStudy)
        .filter(
          (card) =>
            card &&
            !consumedCards.has(cardKey(card)) &&
            (waitingForCard || cardKey(card) !== cardKey(current)),
        );
      lastReview = batch.review_id || "";
      if (waitingForCard) {
        waitingForCard = false;
        if (preparedCards.length) replaceStudy(preparedCards.shift());
        else await updateStudy("/study");
      }
      syncStudyControls();
    } catch (error) {
      if (revision === studyRevision && !studyDeparting && waitingForCard)
        showStudyError(error);
    } finally {
      preparing = null;
    }
  })();
  return preparing;
}

async function saveRatings() {
  if (saving) return saving;
  if (studyFailure) return;
  saving = (async () => {
    try {
      while (outbox.length) {
        const item = outbox[0];
        const response = await studyFetch(item.url, {
          method: "POST",
          body: new URLSearchParams(item.data),
          headers: { "X-Yiju-Buffer": "1" },
        });
        lastReview = (await response.json()).review_id;
        document.dispatchEvent(new Event("yiju:changed"));
        outbox.shift();
        persistOutbox();
        syncStudyControls();
      }
    } catch (error) {
      showStudyError(error);
    } finally {
      saving = null;
    }
    if (
      !studyFailure &&
      !studyDeparting &&
      (preparedCards.length < 6 || waitingForCard)
    ) {
      await prepareStudy();
      if (waitingForCard && !studyFailure) await prepareStudy();
    }
  })();
  return saving;
}

function rateStudy(form, submitter) {
  const session = form.closest("#study-session");
  const key = cardKey(session);
  if (
    consumedCards.has(key) ||
    studyFailure ||
    waitingForCard ||
    studyPending ||
    studyDeparting
  )
    return;
  const item = {
    url: form.action,
    data: Object.fromEntries(new FormData(form, submitter)),
    key,
  };
  outbox.push(item);
  try {
    persistOutbox();
  } catch (error) {
    outbox.pop();
    showStudyError(
      new Error("Allow session storage so ratings can be saved reliably."),
    );
    return;
  }
  document.dispatchEvent(new Event("yiju:changed"));
  consumedCards.add(key);
  studyRevision++;
  if (preparedCards.length) replaceStudy(preparedCards.shift());
  else {
    waitingForCard = true;
    syncStudyControls();
  }
  saveRatings();
}

function updateStudy(url, options = {}) {
  if (studyUpdating) return studyUpdating;
  studyUpdating = performStudyUpdate(url, options).finally(() => {
    studyUpdating = null;
  });
  return studyUpdating;
}

async function performStudyUpdate(url, options = {}) {
  if (studyPending) return;
  studyPending = true;
  studyRevision++;
  syncStudyControls();
  if (outbox.length) await saveRatings();
  if (outbox.length || studyFailure) {
    studyPending = false;
    syncStudyControls();
    return;
  }
  try {
    const response = await studyFetch(url, options);
    if (options.method === "POST")
      document.dispatchEvent(new Event("yiju:changed"));
    const next = parseStudy(await response.text());
    if (!next) throw new Error("Could not load the next card.");
    preparedCards = [];
    consumedCards.clear();
    lastReview = next.querySelector("[name=review_id]")?.value || "";
    waitingForCard = false;
    replaceStudy(next);
  } catch (error) {
    showStudyError(error);
  } finally {
    studyPending = false;
    syncStudyControls();
  }
  if (!studyFailure) prepareStudy();
}

async function retryStudy() {
  studyFailure = false;
  document.querySelector(".study-error").hidden = true;
  syncStudyControls();
  if (outbox.length) await saveRatings();
  else if (waitingForCard) await prepareStudy();
  else await updateStudy("/study");
}

window.addEventListener("beforeunload", (event) => {
  if (outbox.length) {
    event.preventDefault();
    event.returnValue = "";
  }
});

document.addEventListener("click", (event) => {
  const reveal = event.target.closest("#reveal-answer");
  if (reveal) {
    event.preventDefault();
    if (studyPending || waitingForCard || studyFailure || studyDeparting)
      return;
    const session = reveal.closest("#study-session");
    session.querySelector("#answer").hidden = false;
    session.querySelectorAll("[data-reveal-answer]").forEach((element) => {
      element.hidden = false;
    });
    session.querySelectorAll("[data-hide-answer]").forEach((element) => {
      element.hidden = true;
    });
    session.querySelector(".rating-area").hidden = false;
    session.querySelector(".reveal-area").hidden = true;
    session.querySelector(".study-card").classList.add("revealed");
    session.querySelector(".study-scroll").scrollTop = 0;
    document.dispatchEvent(new Event("yiju:revealed"));
    requestAnimationFrame(layoutStudy);
    session.focus({ preventScroll: true });
  }
  const previous = event.target.closest("[data-answer-previous]");
  const next = event.target.closest("[data-answer-next]");
  if (previous || next) {
    const viewport = document.querySelector(".answer-pages");
    viewport.dataset.page = Math.max(
      0,
      Math.min(
        Number(viewport.dataset.pages) - 1,
        Number(viewport.dataset.page) + (next ? 1 : -1),
      ),
    );
    layoutStudy();
  }
  if (event.target.closest("[data-study-refresh]")) {
    event.preventDefault();
    retryStudy();
  }
  if (event.target.closest("[data-study-discard]")) {
    event.preventDefault();
    if (confirm(`Discard ${outbox.length} unconfirmed rating(s) and reload?`)) {
      outbox = [];
      persistOutbox();
      studyFailure = false;
      waitingForCard = false;
      updateStudy("/study");
    }
  }
});
document.addEventListener(
  "toggle",
  (event) => {
    if (event.target.closest(".answer-flow"))
      requestAnimationFrame(layoutStudy);
  },
  true,
);

document.addEventListener("submit", (event) => {
  const form = event.target;
  const session = form.closest("#study-session");
  if (!session) return;
  event.preventDefault();
  if (studyPending || studyFailure || waitingForCard || studyDeparting) return;
  if (form.classList.contains("rating-buttons")) {
    rateStudy(form, event.submitter);
    return;
  }
  updateStudy(form.action, {
    method: "POST",
    body: new FormData(form, event.submitter),
  });
});

document.addEventListener("keydown", (event) => {
  if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
  if (
    event.target.closest(
      "input, textarea, select, button, a, summary, [contenteditable], [data-stroke-character]",
    )
  )
    return;
  const reveal = document.querySelector("#reveal-answer");
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    const button = document.querySelector(
      event.key === "ArrowLeft"
        ? "[data-answer-previous]"
        : "[data-answer-next]",
    );
    if (button && !button.disabled && !button.closest("[hidden]")) {
      event.preventDefault();
      button.click();
    }
    return;
  }
  if (event.code === "Space" && reveal && !reveal.closest("[hidden]")) {
    event.preventDefault();
    reveal.click();
    return;
  }
  if (/^[1-4]$/.test(event.key)) {
    const button = document.querySelector(`[data-rating="${event.key}"]`);
    if (button && !button.disabled && !button.closest("[hidden]")) {
      event.preventDefault();
      button.click();
    }
  }
});
function mountPage() {
  document.querySelectorAll("time[datetime]").forEach((element) => {
    if (element.textContent.includes("UTC")) {
      const date = new Date(element.dateTime);
      if (!Number.isNaN(date.getTime()))
        element.textContent = date.toLocaleString();
    }
  });
  studyDeparting = false;
  if (!document.querySelector("#study-session")) return;
  preparedCards = Array.from(
    document.querySelector("#study-prepared")?.content.children || [],
  );
  consumedCards.clear();
  lastReview = document.querySelector("[name=review_id]")?.value || "";
  waitingForCard = false;
  studyFailure = false;
  studyRevision++;
  initializeStudy();
  try {
    outbox = JSON.parse(sessionStorage.getItem(outboxKey) || "[]");
    outbox.forEach((item) => consumedCards.add(item.key));
    if (outbox.length) {
      waitingForCard = true;
      preparedCards = [];
      syncStudyControls();
      saveRatings();
    }
  } catch (error) {
    showStudyError(
      new Error("Could not recover unsaved ratings from session storage."),
    );
  }
}

window.yijuStudy = {
  async beforeNavigation() {
    studyDeparting = true;
    syncStudyControls();
    if (studyUpdating) await studyUpdating;
    if (saving) await saving;
    if (outbox.length) await saveRatings();
    if (outbox.length) {
      studyDeparting = false;
      syncStudyControls();
      return false;
    }
    return true;
  },
  unmount() {
    document.dispatchEvent(new Event("yiju:unmount"));
    studyRevision++;
    studyObserver?.disconnect();
  },
  cancelNavigation() {
    studyDeparting = false;
    syncStudyControls();
    if (
      document.querySelector("#study-session") &&
      !studyFailure &&
      (waitingForCard || preparedCards.length < 6)
    )
      prepareStudy();
  },
  mount() {
    mountPage();
    document.dispatchEvent(new Event("yiju:mounted"));
  },
};

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-auto-submit]"))
    event.target.form.requestSubmit();
});

mountPage();
