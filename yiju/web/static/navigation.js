(() => {
  const cache = new Map();
  const pendingForms = new WeakSet();
  let generation = 0;
  let navigation = 0;
  let mutation;
  let historyIndex = history.state?.yijuIndex || 0;
  let restoringHistory = false;
  let scrollFrame;

  history.scrollRestoration = "manual";
  history.replaceState(
    { ...history.state, yijuIndex: historyIndex, scroll: [scrollX, scrollY] },
    "",
  );

  function invalidate() {
    generation++;
    cache.clear();
  }

  function appURL(href) {
    const url = new URL(href, location.href);
    if (url.origin !== location.origin || url.hash) return null;
    if (
      !/^\/(?:study|library|sentences(?:\/[^/]+)?|settings|import|imports\/\d+|cards\/[^/]+\/suspend)?$/.test(
        url.pathname,
      )
    )
      return null;
    return url;
  }

  async function requestPage(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { "X-Yiju-Partial": "page" },
    });
    if (!response.ok) {
      const data = response.headers
        .get("Content-Type")
        ?.includes("application/json")
        ? await response.json()
        : {};
      throw new Error(data.error || "Could not load this page.");
    }
    const html = await response.text();
    const main = new DOMParser()
      .parseFromString(html, "text/html")
      .querySelector("#main");
    if (!main?.dataset.title) throw new Error("Could not load this page.");
    return { html, url: response.url };
  }

  function loadPage(url) {
    const key = url.href;
    const cached = cache.get(key);
    const lifetime = url.pathname === "/study" ? 2000 : 15000;
    if (cached && Date.now() - cached.time < lifetime) return cached.promise;
    const entry = { time: Date.now(), promise: requestPage(url) };
    cache.set(key, entry);
    while (cache.size > 12) cache.delete(cache.keys().next().value);
    entry.promise.catch(() => {
      if (cache.get(key) === entry) cache.delete(key);
    });
    return entry.promise;
  }

  function showError(error) {
    let notice = document.querySelector("#navigation-error");
    if (!notice) {
      notice = document.createElement("div");
      notice.id = "navigation-error";
      notice.className = "notice";
      notice.setAttribute("role", "alert");
      document.querySelector("#main").prepend(notice);
    }
    notice.textContent =
      error instanceof TypeError
        ? "Connection lost. Try again."
        : error.message;
  }

  function saveScroll() {
    history.replaceState({ ...history.state, scroll: [scrollX, scrollY] }, "");
  }

  function renderPage(page, options) {
    const main = new DOMParser()
      .parseFromString(page.html, "text/html")
      .querySelector("#main");
    if (options.history !== false) saveScroll();
    window.yijuStudy.unmount();
    document.querySelector("#main").replaceWith(main);
    document.title = main.dataset.title;
    document.body.className = main.dataset.bodyClass;
    document.querySelectorAll(".site-header nav a").forEach((link) => {
      if (link.getAttribute("href") === main.dataset.navigation)
        link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    if (options.history !== false) {
      if (page.url === location.href) {
        history.replaceState(
          { ...history.state, scroll: [0, 0] },
          "",
          page.url,
        );
      } else {
        historyIndex++;
        history.pushState(
          { yijuIndex: historyIndex, scroll: [0, 0] },
          "",
          page.url,
        );
      }
    } else {
      historyIndex = options.state?.yijuIndex ?? historyIndex;
      history.replaceState(history.state, "", page.url);
    }
    window.yijuStudy.mount();
    main.focus({ preventScroll: true });
    const position = options.state?.scroll || [0, 0];
    window.scrollTo({
      left: position[0],
      top: position[1],
      behavior: "instant",
    });
    schedulePrefetch();
  }

  function restoreHistory(options) {
    if (options.history === false && options.state?.yijuIndex !== undefined) {
      const distance = historyIndex - options.state.yijuIndex;
      if (distance) {
        restoringHistory = true;
        history.go(distance);
      }
    }
  }

  async function navigate(url, options = {}) {
    const revision = ++navigation;
    const ready = window.yijuStudy.beforeNavigation();
    let write;
    if (options.method === "POST") {
      const previous = mutation;
      write = (async () => {
        if (!(await ready))
          throw new Error("Save the pending ratings before continuing.");
        if (previous) await previous.catch(() => {});
        invalidate();
        try {
          return await requestPage(url, options);
        } finally {
          invalidate();
        }
      })();
      mutation = write;
    }
    try {
      if (!(await ready)) {
        if (write) await write;
        if (revision === navigation) restoreHistory(options);
        return;
      }
      let page;
      if (write) {
        page = await write;
      } else {
        if (mutation) await mutation.catch(() => {});
        if (revision !== navigation) return;
        let version;
        do {
          version = generation;
          page = await loadPage(url);
        } while (version !== generation && revision === navigation);
      }
      if (revision === navigation) renderPage(page, options);
    } catch (error) {
      if (revision === navigation) {
        restoreHistory(options);
        showError(error);
      }
    } finally {
      if (write && mutation === write) mutation = null;
      if (revision === navigation) window.yijuStudy.cancelNavigation();
    }
  }

  function prefetch(href) {
    const url = appURL(href);
    if (url && url.href !== location.href && !mutation)
      loadPage(url).catch(() => {});
  }

  function schedulePrefetch() {
    const version = generation;
    const schedule =
      window.requestIdleCallback || ((callback) => setTimeout(callback, 100));
    schedule(() => {
      if (version !== generation || mutation) return;
      document
        .querySelectorAll(".site-header nav a")
        .forEach((link) => prefetch(link.href));
    });
  }

  document.addEventListener("click", (event) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    const link = event.target.closest("a[href]");
    if (
      !link ||
      link.hasAttribute("download") ||
      (link.target && link.target !== "_self")
    )
      return;
    const url = appURL(link.href);
    if (!url) return;
    event.preventDefault();
    navigate(url);
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (
      event.defaultPrevented ||
      form.closest("#study-session") ||
      (form.target && form.target !== "_self")
    )
      return;
    const submitter = event.submitter;
    const url = appURL(submitter?.getAttribute("formaction") || form.action);
    if (!url || pendingForms.has(form)) {
      if (pendingForms.has(form)) event.preventDefault();
      return;
    }
    const method = (
      submitter?.getAttribute("formmethod") || form.method
    ).toUpperCase();
    if (!["GET", "POST"].includes(method)) return;
    event.preventDefault();
    const data = new FormData(form, submitter);
    if (method === "GET") {
      url.search = new URLSearchParams(data).toString();
      navigate(url);
      return;
    }
    pendingForms.add(form);
    const buttons = Array.from(form.querySelectorAll("button"));
    const disabled = buttons.map((button) => button.disabled);
    buttons.forEach((button) => {
      button.disabled = true;
    });
    form.setAttribute("aria-busy", "true");
    const message = form.querySelector(".busy-message");
    if (message) message.hidden = false;
    await navigate(url, { method, body: data });
    pendingForms.delete(form);
    buttons.forEach((button, index) => {
      button.disabled = disabled[index];
    });
    form.removeAttribute("aria-busy");
    if (message) message.hidden = true;
  });

  ["pointerover", "focusin", "touchstart"].forEach((type) => {
    document.addEventListener(
      type,
      (event) => {
        const link = event.target.closest("a[href]");
        if (link) prefetch(link.href);
      },
      { passive: true },
    );
  });

  window.addEventListener("popstate", (event) => {
    if (restoringHistory) {
      restoringHistory = false;
      return;
    }
    navigate(new URL(location.href), { history: false, state: event.state });
  });
  window.addEventListener(
    "scroll",
    () => {
      if (scrollFrame) cancelAnimationFrame(scrollFrame);
      scrollFrame = requestAnimationFrame(saveScroll);
    },
    { passive: true },
  );
  document.addEventListener("yiju:changed", invalidate);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) invalidate();
  });
  schedulePrefetch();
})();
