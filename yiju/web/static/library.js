(() => {
  const pending = new WeakSet();

  async function loadExamples(details) {
    if (!details.open || details.dataset.examplesLoaded || pending.has(details))
      return;
    pending.add(details);
    const content = details.querySelector("[data-library-example-content]");
    const status = details.querySelector("[data-library-example-status]");
    const retry = details.querySelector("[data-library-example-retry]");
    details.setAttribute("aria-busy", "true");
    status.hidden = false;
    status.textContent = "Loading…";
    retry.hidden = true;
    try {
      const response = await fetch(details.dataset.libraryExamples, {
        headers: { "X-Yiju-Partial": "examples" },
      });
      if (!response.ok) throw new Error("Could not load examples.");
      const html = await response.text();
      if (!details.isConnected) return;
      content.innerHTML = html;
      document.dispatchEvent(new Event("yiju:pronunciation-added"));
      details.dataset.examplesLoaded = "true";
      status.hidden = true;
    } catch {
      if (!details.isConnected) return;
      status.textContent = "Could not load examples.";
      retry.hidden = false;
    } finally {
      details.removeAttribute("aria-busy");
      pending.delete(details);
    }
  }

  document.addEventListener(
    "toggle",
    (event) => {
      const details = event.target;
      if (details.matches("details[data-library-examples]"))
        loadExamples(details);
    },
    true,
  );
  document.addEventListener("click", (event) => {
    const retry = event.target.closest("[data-library-example-retry]");
    if (retry) loadExamples(retry.closest("details[data-library-examples]"));
  });
})();
