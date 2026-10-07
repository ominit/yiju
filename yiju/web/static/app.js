let studyPending = false;

async function updateStudy(url, options = {}) {
  if (studyPending) return;
  const session = document.querySelector("#study-session");
  if (!session) return;
  studyPending = true;
  session.setAttribute("aria-busy", "true");
  session.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });
  try {
    const response = await fetch(url, {
      ...options,
      headers: { "X-Yiju-Partial": "study" },
    });
    if (!response.ok) {
      if (!response.headers.get("Content-Type")?.includes("application/json")) {
        throw new Error(
          "Could not update this session. Reload it before continuing.",
        );
      }
      const error = await response.json();
      throw new Error(error.error || "Could not update this session.");
    }
    const page = new DOMParser().parseFromString(
      await response.text(),
      "text/html",
    );
    const next = page.querySelector("#study-session");
    if (!next) throw new Error("Could not load the next card.");
    session.replaceWith(next);
    history.replaceState(null, "", "/study");
    next.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
  } catch (error) {
    const notice = session.querySelector(".study-error");
    notice.querySelector("p").textContent =
      error instanceof TypeError
        ? "Connection lost. Reload this session before continuing."
        : error.message;
    notice.hidden = false;
    session.dataset.needsReload = "true";
  } finally {
    studyPending = false;
    session.removeAttribute("aria-busy");
    session.querySelectorAll("button").forEach((button) => {
      button.disabled =
        session.dataset.needsReload === "true" &&
        !button.hasAttribute("data-study-refresh");
    });
  }
}

document.addEventListener("click", (event) => {
  const reveal = event.target.closest("#reveal-answer");
  if (reveal) {
    event.preventDefault();
    if (studyPending) return;
    const session = reveal.closest("#study-session");
    session.querySelector("#answer").hidden = false;
    session.querySelector(".rating-area").hidden = false;
    session.querySelector(".reveal-area").hidden = true;
    session.querySelector(".study-card").classList.add("revealed");
    session.focus({ preventScroll: true });
  }
  if (event.target.closest("[data-study-refresh]")) {
    event.preventDefault();
    updateStudy("/study");
  }
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  const session = form.closest("#study-session");
  if (!session) return;
  event.preventDefault();
  if (studyPending || session.dataset.needsReload === "true") return;
  updateStudy(form.action, {
    method: "POST",
    body: new FormData(form, event.submitter),
  });
});

document.addEventListener("keydown", (event) => {
  if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
  if (
    event.target.closest(
      "input, textarea, select, button, a, summary, [contenteditable]",
    )
  )
    return;
  const reveal = document.querySelector("#reveal-answer");
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
document.querySelectorAll("form[data-busy]").forEach((form) => {
  form.addEventListener("submit", () => {
    form.querySelector('button[type="submit"]').disabled = true;
    form.querySelector(".busy-message").hidden = false;
  });
});
document.querySelectorAll("time[datetime]").forEach((element) => {
  if (element.textContent.includes("UTC")) {
    const date = new Date(element.dateTime);
    if (!Number.isNaN(date.getTime()))
      element.textContent = date.toLocaleString();
  }
});
