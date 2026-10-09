(() => {
  const dataCache = new Map();
  const readyData = new Map();
  const glyphs = new Map();
  let bufferedCharacters = new Set();
  const waitingLoads = [];
  const svgNS = "http://www.w3.org/2000/svg";
  let loading = 0;
  let writer;
  let animation;
  let active;
  let pointer;
  let lastClick;
  let clickTimer;
  let finishTimer;

  function stop() {
    clearTimeout(clickTimer);
    clearTimeout(finishTimer);
    const previous = active;
    active = null;
    previous?.element.classList.remove("stroke-animating");
    animation?.remove();
    if (previous?.playing)
      writer.showCharacter({ duration: 0 }).catch(() => {});
  }

  async function fetchCharacter(character) {
    if (loading < 2) loading++;
    else
      await new Promise((resolve) => {
        waitingLoads.push({ character, resolve });
      });
    try {
      if (
        !bufferedCharacters.has(character) &&
        ![...glyphs.values()].some(
          (state) => state.character === character && currentGlyph(state),
        )
      )
        throw new DOMException("Character is no longer needed.", "AbortError");
      const response = await fetch(
        `/strokes/characters/${encodeURIComponent(character)}`,
        { headers: { "X-Yiju-Partial": "page" } },
      );
      const data = await response.json();
      if (!response.ok)
        throw new Error(data.error || "Stroke order is unavailable.");
      return data;
    } finally {
      const priority = waitingLoads.findIndex(({ character }) =>
        [...glyphs.values()].some(
          (state) => state.character === character && currentGlyph(state),
        ),
      );
      const next = waitingLoads.splice(Math.max(0, priority), 1)[0];
      if (next) next.resolve();
      else loading--;
    }
  }

  function characterData(character) {
    if (!dataCache.has(character)) {
      const promise = fetchCharacter(character)
        .then((data) => {
          readyData.set(character, data);
          return data;
        })
        .catch((error) => {
          dataCache.delete(character);
          if (error instanceof TypeError)
            throw new Error("Stroke order is unavailable. Tap to retry.");
          throw error;
        });
      dataCache.set(character, promise);
    }
    return dataCache.get(character);
  }

  function currentGlyph(state) {
    return glyphs.get(state.element) === state && state.element.isConnected;
  }

  function renderGlyph(state, data) {
    clearTimeout(state.fallbackTimer);
    state.stableFallback = false;
    if (!state.element.querySelector(".stroke-glyph")) {
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("viewBox", "0 0 100 100");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      svg.classList.add("stroke-glyph");
      const group = document.createElementNS(svgNS, "g");
      group.setAttribute(
        "transform",
        HanziWriter.getScalingTransform(100, 100, 5).transform,
      );
      group.setAttribute("fill", "currentColor");
      for (const stroke of data.strokes) {
        const path = document.createElementNS(svgNS, "path");
        path.setAttribute("d", stroke);
        group.append(path);
      }
      svg.append(group);
      state.element.append(svg);
    }
    state.element.classList.add("stroke-rendered");
    state.element.classList.remove("stroke-pending");
  }

  function showFallback(state) {
    if (!currentGlyph(state)) return;
    clearTimeout(state.fallbackTimer);
    state.stableFallback = true;
    state.element.classList.remove("stroke-pending");
  }

  function pendingGlyph(state) {
    if (
      state.stableFallback ||
      state.element.classList.contains("stroke-rendered") ||
      state.element.closest("[hidden]") ||
      state.fallbackTimer
    )
      return;
    state.fallbackTimer = setTimeout(() => showFallback(state), 150);
  }

  function loadGlyph(state) {
    if (state.data) return Promise.resolve(state.data);
    if (state.loading) return state.loading;
    state.loading = characterData(state.character)
      .then((data) => {
        if (!currentGlyph(state)) return data;
        state.data = data;
        if (!state.stableFallback) renderGlyph(state, data);
        return data;
      })
      .finally(() => {
        state.loading = null;
      });
    return state.loading;
  }

  function currentAnimation(request) {
    return (
      active === request &&
      currentGlyph(request.state) &&
      request.element.closest(".study-card.revealed")
    );
  }

  async function play(element) {
    const state = glyphs.get(element);
    if (!state || !element.closest(".study-card.revealed")) return;
    stop();
    const request = { element, state, playing: false };
    active = request;
    const feedback = document.querySelector("[data-stroke-feedback]");
    feedback.hidden = true;
    try {
      await loadGlyph(state);
      if (!currentAnimation(request)) return;
      renderGlyph(state, state.data);
      const styles = getComputedStyle(element.closest(".chinese"));
      const strokeColor = styles.color;
      const outlineColor = styles.getPropertyValue("--line").trim();
      if (!writer) {
        animation = document.createElement("span");
        animation.className = "stroke-animation";
        animation.setAttribute("aria-hidden", "true");
        writer = HanziWriter.create(animation, state.character, {
          width: 100,
          height: 100,
          padding: 5,
          showCharacter: false,
          showOutline: true,
          strokeColor,
          radicalColor: strokeColor,
          outlineColor,
          strokeAnimationSpeed: 1.2,
          delayBetweenStrokes: 180,
          charDataLoader: (character, loaded, failed) => {
            characterData(character).then(loaded, failed);
          },
        });
        const svg = animation.querySelector("svg");
        svg.setAttribute("viewBox", "0 0 100 100");
        svg.setAttribute("focusable", "false");
      } else {
        await writer.setCharacter(state.character);
      }
      await writer.getCharacterData();
      if (!currentAnimation(request)) return;
      await Promise.all([
        writer.updateColor("strokeColor", strokeColor, { duration: 0 }),
        writer.updateColor("radicalColor", strokeColor, { duration: 0 }),
        writer.updateColor("outlineColor", outlineColor, { duration: 0 }),
        writer.hideCharacter({ duration: 0 }),
        writer.showOutline({ duration: 0 }),
      ]);
      if (!currentAnimation(request)) return;
      element.append(animation);
      element.classList.add("stroke-animating");
      request.playing = true;
      await writer.animateCharacter();
      if (!currentAnimation(request)) return;
      finishTimer = setTimeout(() => {
        if (active !== request) return;
        active = null;
        element.classList.remove("stroke-animating");
        animation.remove();
      }, 350);
    } catch (error) {
      if (!currentAnimation(request)) return;
      stop();
      feedback.textContent =
        error.message || "Stroke order is unavailable. Tap to retry.";
      feedback.hidden = false;
    }
  }

  function syncCharacters() {
    const card = document.querySelector(".study-card");
    if (!card) return;
    const revealed = card.classList.contains("revealed");
    card.querySelectorAll(".question .character").forEach((element) => {
      const character = element.textContent;
      if (!/^\p{Script=Han}$/u.test(character)) return;
      let state = glyphs.get(element);
      if (!state) {
        state = { element, character };
        glyphs.set(element, state);
        const data = readyData.get(character);
        if (data) {
          state.data = data;
          renderGlyph(state, data);
        } else {
          if (!element.classList.contains("stroke-rendered"))
            element.classList.add("stroke-pending");
          loadGlyph(state).catch(() => showFallback(state));
        }
      }
      pendingGlyph(state);
      if (revealed) {
        element.dataset.strokeCharacter = character;
        element.tabIndex = 0;
        element.setAttribute("role", "button");
        element.setAttribute(
          "aria-label",
          `Play stroke order for ${character}`,
        );
        element.title = "Play stroke order";
      }
    });
  }

  function unmount() {
    stop();
    pointer = null;
    lastClick = null;
    bufferedCharacters.clear();
    for (const { element } of glyphs.values()) {
      element.removeAttribute("data-stroke-character");
      element.removeAttribute("tabindex");
      element.removeAttribute("role");
      element.removeAttribute("aria-label");
      element.removeAttribute("title");
    }
    for (const state of glyphs.values()) clearTimeout(state.fallbackTimer);
    glyphs.clear();
  }

  function prepareCharacters(cards) {
    bufferedCharacters = new Set(
      cards.flatMap((card) =>
        [...card.querySelectorAll(".question .character")]
          .map((element) => element.textContent)
          .filter((character) => /^\p{Script=Han}$/u.test(character)),
      ),
    );
    for (const character of bufferedCharacters)
      characterData(character).catch(() => {});
  }

  function mount() {
    unmount();
    syncCharacters();
    prepareCharacters(window.yijuStudy?.preparedCards() || []);
  }

  document.addEventListener("yiju:mounted", mount);
  document.addEventListener("yiju:revealed", syncCharacters);
  document.addEventListener("yiju:prepared", (event) => {
    prepareCharacters(event.detail.cards);
  });
  document.addEventListener("yiju:unmount", unmount);
  document.addEventListener("pointerdown", (event) => {
    clearTimeout(clickTimer);
    const element = event.target.closest("[data-stroke-character]");
    pointer =
      element && event.isPrimary && event.button === 0
        ? {
            element,
            id: event.pointerId,
            x: event.clientX,
            y: event.clientY,
            started: performance.now(),
            moved: false,
          }
        : null;
  });
  document.addEventListener("pointermove", (event) => {
    if (
      pointer?.id === event.pointerId &&
      Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) > 6
    )
      pointer.moved = true;
  });
  document.addEventListener("pointercancel", () => {
    pointer = null;
    lastClick = null;
    clearTimeout(clickTimer);
  });
  document.addEventListener("click", (event) => {
    const element = event.target.closest("[data-stroke-character]");
    if (!element) return;
    const gesture = pointer;
    pointer = null;
    clearTimeout(clickTimer);
    if (
      event.altKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.shiftKey ||
      !getSelection()?.isCollapsed
    ) {
      stop();
      lastClick = null;
      return;
    }
    if (event.detail === 0) {
      play(element);
      return;
    }
    if (
      gesture?.element !== element ||
      gesture.moved ||
      performance.now() - gesture.started > 450
    ) {
      lastClick = null;
      return;
    }
    const repeated =
      event.detail > 1 &&
      lastClick?.element === element &&
      performance.now() - lastClick.time < 600;
    lastClick = { element, time: performance.now() };
    if (repeated) {
      stop();
      return;
    }
    clickTimer = setTimeout(() => {
      if (getSelection()?.isCollapsed) play(element);
    }, 250);
  });
  document.addEventListener("selectionchange", () => {
    if (!getSelection()?.isCollapsed) {
      lastClick = null;
      stop();
    }
  });
  document.addEventListener("contextmenu", (event) => {
    if (event.target.closest(".pronunciation")) stop();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      stop();
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
    const element = event.target.closest("[data-stroke-character]");
    if (element && (event.key === "Enter" || event.code === "Space")) {
      event.preventDefault();
      play(element);
    }
  });

  mount();
})();
