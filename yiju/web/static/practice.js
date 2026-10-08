(() => {
  const flags = new Map();
  const pendingFlags = new Set();
  let muted = readPreference("yiju-muted") === "true";
  let player;
  let audioURL = "";
  let board;
  let boardObserver;
  let pronunciationObserver;
  let pronunciationFrame;

  function layoutPronunciation() {
    pronunciationFrame = null;
    document.querySelectorAll(".pronunciation").forEach((element) => {
      const reading = element.querySelector(".pronunciation-pinyin");
      if (!reading || reading.hidden || !element.getBoundingClientRect().width)
        return;
      const characters = element.querySelectorAll(".character");
      reading.querySelectorAll(".syllable").forEach((syllable) => {
        characters[Number(syllable.dataset.characterIndex)].style.minWidth =
          `${syllable.offsetWidth}px`;
      });
      const origin = element.getBoundingClientRect();
      reading.querySelectorAll(".syllable").forEach((syllable) => {
        const character = characters[Number(syllable.dataset.characterIndex)];
        const rect = character.getBoundingClientRect();
        syllable.style.left = `${rect.left + rect.width / 2 - origin.left}px`;
        syllable.style.top = `${rect.top - origin.top - syllable.offsetHeight}px`;
      });
    });
  }

  function schedulePronunciation() {
    pronunciationFrame ||= requestAnimationFrame(layoutPronunciation);
  }

  function mountPronunciation() {
    pronunciationObserver?.disconnect();
    pronunciationObserver = new ResizeObserver(schedulePronunciation);
    document.querySelectorAll(".pronunciation").forEach((element) => {
      pronunciationObserver.observe(element);
    });
    schedulePronunciation();
  }

  function readPreference(key) {
    try {
      return sessionStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function storePreference(key, value) {
    try {
      sessionStorage.setItem(key, String(value));
    } catch {}
  }

  function notice(message) {
    let element = document.querySelector("#practice-notice");
    if (!element) {
      element = document.createElement("div");
      element.id = "practice-notice";
      element.className = "notice";
      element.setAttribute("role", "alert");
      document.querySelector("#main").prepend(element);
    }
    element.textContent = message;
  }

  function syncFlags() {
    document.querySelectorAll("[data-flag-word]").forEach((button) => {
      const word = button.dataset.flagWord;
      if (!flags.has(word))
        flags.set(word, button.getAttribute("aria-pressed") === "true");
      const flagged = flags.get(word);
      button.setAttribute("aria-pressed", String(flagged));
      button.setAttribute(
        "aria-label",
        `${flagged ? "Unflag" : "Flag"} ${word}`,
      );
      button.title = `${flagged ? "Unflag" : "Flag for correction"} ${word}`;
      button.disabled = pendingFlags.has(word);
    });
  }

  async function saveFlag(word, flagged, csrf, note) {
    const body = new URLSearchParams({
      word,
      flagged: flagged ? "1" : "0",
      csrf,
    });
    if (note !== undefined) body.set("note", note);
    const response = await fetch("/flags/words", {
      method: "POST",
      keepalive: true,
      body,
      headers: { "X-Yiju-Partial": "page" },
    });
    const data = await response.json();
    if (!response.ok)
      throw new Error(data.error || "Could not save this flag.");
    flags.set(word, data.flagged);
    document.dispatchEvent(new Event("yiju:changed"));
  }

  function stopAudio() {
    if (player) {
      player.pause();
      player.removeAttribute("src");
      player.load();
    }
    player = null;
    audioURL = "";
  }

  function syncAudio() {
    document.querySelectorAll("[data-audio-toggle]").forEach((button) => {
      button.setAttribute("aria-pressed", String(muted));
      button.setAttribute(
        "aria-label",
        muted ? "Unmute pronunciation" : "Mute pronunciation",
      );
      button.title = muted ? "Unmute pronunciation" : "Mute pronunciation";
      button.querySelector("[data-audio-on]").hidden = muted;
      button.querySelector("[data-audio-off]").hidden = !muted;
    });
    const session = document.querySelector("#study-session");
    const replay = session?.querySelector("[data-audio-replay]");
    if (replay) {
      replay.hidden = !session.querySelector(".study-card.revealed");
      replay.disabled = muted;
    }
  }

  function prepareAudio() {
    const url = document.querySelector("#study-session")?.dataset.audioUrl;
    if (muted || !url) return;
    if (url === audioURL && player) return;
    stopAudio();
    audioURL = url;
    player = new Audio(url);
    player.preload = "auto";
    player.load();
  }

  function playAudio() {
    if (muted) return;
    prepareAudio();
    if (!player) return;
    const current = player;
    if (current.readyState) current.currentTime = 0;
    current.play().catch((error) => {
      if (current !== player || muted || error.name === "AbortError") return;
      const button = document.querySelector("[data-audio-replay]");
      if (button) {
        button.title = "Audio unavailable. Click to retry.";
        button.setAttribute("aria-label", "Retry pronunciation");
        button.dataset.audioFailed = "true";
      }
    });
  }

  function paintDrawing(canvas, drawing, width, height, strokeWidth) {
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const context = canvas.getContext("2d");
    context.scale(ratio, ratio);
    const space = drawing.space || [width, height];
    const scale = Math.min(width / space[0], height / space[1]);
    context.translate(
      (width - space[0] * scale) / 2,
      (height - space[1] * scale) / 2,
    );
    context.scale(scale, scale);
    context.strokeStyle = getComputedStyle(canvas).color;
    context.lineWidth = strokeWidth / scale;
    context.lineCap = "round";
    context.lineJoin = "round";
    drawing.strokes.forEach((stroke) => {
      context.beginPath();
      const first = stroke[0];
      context.moveTo(first[0] * space[0], first[1] * space[1]);
      stroke
        .slice(1)
        .forEach(([x, y]) => context.lineTo(x * space[0], y * space[1]));
      if (stroke.length === 1)
        context.lineTo(
          first[0] * space[0] + 0.1 / scale,
          first[1] * space[1] + 0.1 / scale,
        );
      context.stroke();
    });
  }

  function positionDrawingHistory(history) {
    if (
      matchMedia(
        "(max-width: 700px), (max-width: 1000px) and (max-height: 500px)",
      ).matches
    ) {
      history.dataset.placement = "above";
      history.style.removeProperty("--preview-size");
      return;
    }
    const canvas = board.canvas.getBoundingClientRect();
    const previewMax = parseFloat(
      getComputedStyle(board.canvas.closest(".drawing-board")).getPropertyValue(
        "--preview-max",
      ),
    );
    const size = Math.min(previewMax, Math.max(24, canvas.height - 2));
    const left = canvas.left - size - 20;
    const overlaps =
      left < 8 ||
      [
        ...document.querySelectorAll(
          ".question .character, .question .syllable, .recall-prompt:not([hidden]), .recall-definitions:not([hidden])",
        ),
      ].some((element) => {
        const rect = element.getBoundingClientRect();
        return (
          rect.width &&
          rect.left < canvas.left - 8 &&
          rect.right > left &&
          rect.top < canvas.bottom &&
          rect.bottom > canvas.top
        );
      });
    if (!overlaps) {
      delete history.dataset.placement;
      history.style.removeProperty("--preview-size");
      return;
    }
    const heading = document
      .querySelector(".card-heading")
      .getBoundingClientRect();
    const progress = document
      .querySelector(".card-progress")
      .getBoundingClientRect();
    const controls = document
      .querySelector(".practice-controls")
      .getBoundingClientRect();
    const width = controls.left - progress.right - 16;
    const previewSize = Math.min(previewMax, heading.height - 8, width);
    history.dataset.placement = "toolbar";
    history.style.setProperty("--preview-size", `${previewSize}px`);
    history.style.setProperty(
      "--history-left",
      `${progress.right + 8 - canvas.left}px`,
    );
    history.style.setProperty("--history-top", `${heading.top - canvas.top}px`);
    history.style.setProperty("--history-width", `${width}px`);
  }

  function renderDrawingHistory() {
    if (!board) return;
    const history = document.querySelector("[data-drawing-history]");
    if (!board.canvas.getBoundingClientRect().width) return;
    positionDrawingHistory(history);
    history.replaceChildren();
    for (const drawing of board.characters.slice(0, board.index)) {
      if (!drawing.strokes.length) continue;
      const preview = document.createElement("canvas");
      preview.setAttribute("role", "img");
      preview.setAttribute("aria-label", "Previously drawn character");
      history.append(preview);
      const size = parseFloat(getComputedStyle(preview).width);
      paintDrawing(preview, drawing, size, size, 1.5);
    }
    history.hidden = !history.childElementCount;
    history.scrollTop = history.scrollHeight;
    history.scrollLeft = history.scrollWidth;
    document.dispatchEvent(new Event("yiju:drawing-history"));
  }

  function renderBoard() {
    if (!board) return;
    const drawing = board.characters[board.index];
    const canvas = board.canvas;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    paintDrawing(canvas, drawing, rect.width, rect.height, 3);
    const container = canvas.closest(".drawing-board");
    container.querySelector("[data-drawing-undo]").disabled =
      !drawing.strokes.length;
    container.querySelector("[data-drawing-clear]").disabled =
      !drawing.strokes.length;
    container.querySelector("[data-drawing-previous]").disabled =
      board.index === 0;
  }

  function mountBoard() {
    boardObserver?.disconnect();
    board = null;
    const canvas = document.querySelector("[data-drawing-canvas]");
    if (!canvas) return;
    board = {
      canvas,
      characters: [{ strokes: [], space: null }],
      index: 0,
      pointer: null,
    };
    boardObserver = new ResizeObserver(() => {
      renderBoard();
      renderDrawingHistory();
    });
    boardObserver.observe(canvas);
    boardObserver.observe(canvas.closest(".drawing-stage"));
    const point = (event) => {
      const drawing = board.characters[board.index];
      const rect = canvas.getBoundingClientRect();
      drawing.space ||= [rect.width, rect.height];
      const scale = Math.min(
        rect.width / drawing.space[0],
        rect.height / drawing.space[1],
      );
      const left = (rect.width - drawing.space[0] * scale) / 2;
      const top = (rect.height - drawing.space[1] * scale) / 2;
      return [
        Math.max(
          0,
          Math.min(
            1,
            (event.clientX - rect.left - left) / (scale * drawing.space[0]),
          ),
        ),
        Math.max(
          0,
          Math.min(
            1,
            (event.clientY - rect.top - top) / (scale * drawing.space[1]),
          ),
        ),
      ];
    };
    canvas.addEventListener("pointerdown", (event) => {
      if (
        board?.canvas !== canvas ||
        event.button !== 0 ||
        board.pointer !== null
      )
        return;
      event.preventDefault();
      board.pointer = event.pointerId;
      canvas.setPointerCapture(event.pointerId);
      board.characters[board.index].strokes.push([point(event)]);
      renderBoard();
    });
    canvas.addEventListener("pointermove", (event) => {
      if (board?.canvas !== canvas || board.pointer !== event.pointerId) return;
      event.preventDefault();
      const events = event.getCoalescedEvents?.() || [event];
      const stroke = board.characters[board.index].strokes.at(-1);
      events.forEach((sample) => stroke.push(point(sample)));
      renderBoard();
    });
    const endStroke = (event) => {
      if (board?.canvas !== canvas || board.pointer !== event.pointerId) return;
      board.pointer = null;
      if (canvas.hasPointerCapture(event.pointerId))
        canvas.releasePointerCapture(event.pointerId);
    };
    canvas.addEventListener("pointerup", endStroke);
    canvas.addEventListener("pointercancel", endStroke);
    canvas.addEventListener("lostpointercapture", endStroke);
    requestAnimationFrame(renderBoard);
  }

  function mount(event) {
    if (!event?.detail?.studyOnly)
      for (const word of flags.keys())
        if (!pendingFlags.has(word)) flags.delete(word);
    document.querySelector("#practice-notice")?.remove();
    syncFlags();
    const filter = document.querySelector(".library-filters [aria-current]");
    if (filter)
      filter.parentElement.scrollTo({
        left: Math.max(
          0,
          filter.offsetLeft - filter.parentElement.offsetLeft - 12,
        ),
        behavior: "instant",
      });
    syncAudio();
    stopAudio();
    prepareAudio();
    mountBoard();
    mountPronunciation();
  }

  document.addEventListener("yiju:mounted", mount);
  document.addEventListener("yiju:unmount", () => {
    stopAudio();
    boardObserver?.disconnect();
    pronunciationObserver?.disconnect();
    cancelAnimationFrame(pronunciationFrame);
    pronunciationFrame = null;
    board = null;
  });
  document.addEventListener("yiju:revealed", () => {
    syncAudio();
    playAudio();
    schedulePronunciation();
    requestAnimationFrame(renderBoard);
  });
  document.fonts.addEventListener("loadingdone", schedulePronunciation);
  document.fonts.ready.then(schedulePronunciation);

  document.addEventListener("copy", (event) => {
    const selection = getSelection();
    if (!event.clipboardData || !selection?.rangeCount || selection.isCollapsed)
      return;
    const range = selection.getRangeAt(0);
    const selector = ".pronunciation-characters, .pronunciation-pinyin";
    const start = range.startContainer.parentElement?.closest(selector);
    const end = range.endContainer.parentElement?.closest(selector);
    const group =
      range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
        ? range.commonAncestorContainer.closest?.(selector)
        : start;
    if (!group || (start && end && start !== end)) return;
    let text = range.cloneContents().textContent;
    if (group.matches(".pronunciation-pinyin"))
      text = text.replace(/\s+/g, " ").trim();
    event.clipboardData.setData("text/plain", text);
    event.preventDefault();
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.matches("[data-flag-word]")) {
      event.preventDefault();
      const word = button.dataset.flagWord;
      if (pendingFlags.has(word)) return;
      const previous = flags.get(word);
      flags.set(word, !previous);
      pendingFlags.add(word);
      syncFlags();
      try {
        await saveFlag(word, !previous, button.dataset.csrf);
      } catch (error) {
        flags.set(word, previous);
        notice(error.message || "Could not save this flag.");
      } finally {
        pendingFlags.delete(word);
        syncFlags();
      }
    }
    if (button.matches("[data-audio-toggle]")) {
      muted = !muted;
      storePreference("yiju-muted", muted);
      syncAudio();
      if (muted) stopAudio();
      else if (document.querySelector(".study-card.revealed")) playAudio();
      else prepareAudio();
    }
    if (button.matches("[data-audio-replay]")) {
      if (button.dataset.audioFailed) {
        stopAudio();
        delete button.dataset.audioFailed;
      }
      playAudio();
    }
    if (
      button.matches(
        "[data-drawing-undo], [data-drawing-clear], [data-drawing-next], [data-drawing-previous]",
      ) &&
      board
    ) {
      const pointer = board.pointer;
      board.pointer = null;
      if (pointer !== null && board.canvas.hasPointerCapture(pointer))
        board.canvas.releasePointerCapture(pointer);
    }
    if (button.matches("[data-drawing-undo]") && board) {
      const drawing = board.characters[board.index];
      drawing.strokes.pop();
      if (!drawing.strokes.length) drawing.space = null;
      renderBoard();
    }
    if (button.matches("[data-drawing-clear]") && board) {
      board.characters[board.index] = { strokes: [], space: null };
      renderBoard();
    }
    if (button.matches("[data-drawing-next]") && board) {
      board.index++;
      board.characters[board.index] ||= { strokes: [], space: null };
      renderBoard();
      renderDrawingHistory();
    }
    if (button.matches("[data-drawing-previous]") && board) {
      board.index = Math.max(0, board.index - 1);
      renderBoard();
      renderDrawingHistory();
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("[data-flag-note]")) return;
    event.preventDefault();
    const button = form.querySelector("button");
    const status = form.querySelector("[data-note-status]");
    button.disabled = true;
    status.textContent = "";
    try {
      await saveFlag(
        form.dataset.word,
        true,
        form.elements.csrf.value,
        form.elements.note.value,
      );
      status.textContent = "Saved";
    } catch (error) {
      status.textContent = error.message || "Could not save this note.";
    } finally {
      button.disabled = false;
    }
  });

  mount();
})();
