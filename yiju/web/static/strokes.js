(() => {
  const dataCache = new Map();
  const glyphs = new Map();
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
    else await new Promise((resolve) => waitingLoads.push(resolve));
    try {
      if (
        ![...glyphs.values()].some(
          (state) => state.character === character && currentGlyph(state),
        )
      )
        throw new DOMException("Character is no longer visible.", "AbortError");
      const response = await fetch(
        `/strokes/characters/${encodeURIComponent(character)}`,
        { headers: { "X-Yiju-Partial": "page" } },
      );
      const data = await response.json();
      if (!response.ok)
        throw new Error(data.error || "Stroke order is unavailable.");
      return data;
    } finally {
      const next = waitingLoads.shift();
      if (next) next();
      else loading--;
    }
  }

  function characterData(character) {
    if (!dataCache.has(character)) {
      const promise = fetchCharacter(character).catch((error) => {
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
    return (
      glyphs.get(state.element) === state &&
      state.element.isConnected &&
      !state.element.closest("[hidden]")
    );
  }

  function loadGlyph(state) {
    if (state.data) return Promise.resolve(state.data);
    if (state.loading) return state.loading;
    state.loading = characterData(state.character)
      .then((data) => {
        if (!currentGlyph(state)) return data;
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
        state.element.classList.add("stroke-rendered");
        state.data = data;
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
      const styles = getComputedStyle(element.closest(".chinese"));
      await Promise.all([
        writer.updateColor("strokeColor", styles.color, { duration: 0 }),
        writer.updateColor(
          "outlineColor",
          styles.getPropertyValue("--line").trim(),
          { duration: 0 },
        ),
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
      if (element.closest("[hidden]") || !/^\p{Script=Han}$/u.test(character))
        return;
      let state = glyphs.get(element);
      if (!state) {
        state = { element, character };
        glyphs.set(element, state);
        loadGlyph(state).catch(() => {});
      }
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
    for (const { element } of glyphs.values()) {
      element.querySelector(".stroke-glyph")?.remove();
      element.classList.remove("stroke-rendered");
      element.removeAttribute("data-stroke-character");
      element.removeAttribute("tabindex");
      element.removeAttribute("role");
      element.removeAttribute("aria-label");
      element.removeAttribute("title");
    }
    glyphs.clear();
  }

  function mount() {
    unmount();
    syncCharacters();
  }

  document.addEventListener("yiju:mounted", mount);
  document.addEventListener("yiju:revealed", syncCharacters);
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
