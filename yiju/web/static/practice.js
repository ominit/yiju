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
      const session = button.closest("#study-session");
      const concealed =
        session &&
        session.dataset.direction !== "recognition" &&
        !session.querySelector(".study-card.revealed");
      button.setAttribute("aria-pressed", String(flagged));
      button.setAttribute(
        "aria-label",
        `${flagged ? "Unflag" : "Flag"} ${concealed ? "card" : word}`,
      );
      button.title = `${flagged ? "Unflag" : "Flag for correction"}${concealed ? "" : ` ${word}`}`;
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
    const current = player;
    player.addEventListener("playing", () => {
      if (current !== player) return;
      listeningStatus("Playing…");
      document.querySelectorAll("[data-audio-failed]").forEach((button) => {
        delete button.dataset.audioFailed;
      });
    });
    player.addEventListener("ended", () => {
      if (current === player) listeningStatus("");
    });
    player.addEventListener("error", () => {
      if (current === player)
        listeningStatus("Audio unavailable. Tap to retry.");
    });
    player.load();
  }

  function listeningStatus(message) {
    const status = document.querySelector("[data-listening-status]");
    if (!status) return;
    status.hidden = !message;
    status.textContent = message;
  }

  function playAudio() {
    if (muted) return;
    prepareAudio();
    if (!player) return;
    const current = player;
    if (current.readyState) current.currentTime = 0;
    current.play().catch((error) => {
      if (current !== player || muted || error.name === "AbortError") return;
      if (error.name === "NotAllowedError") {
        listeningStatus("");
        return;
      }
      listeningStatus("Audio unavailable. Tap to retry.");
      document
        .querySelectorAll("[data-audio-replay], [data-listening-play]")
        .forEach((button) => {
          button.title = "Audio unavailable. Click to retry.";
          button.setAttribute("aria-label", "Retry pronunciation");
          button.dataset.audioFailed = "true";
        });
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

  function drawingPreview(drawing, index) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "drawing-thumbnail";
    button.dataset.drawingSelect = String(index);
    button.setAttribute("aria-label", `Edit drawn character ${index + 1}`);
    button.setAttribute("aria-pressed", String(index === board.index));
    const namespace = "http://www.w3.org/2000/svg";
    const preview = document.createElementNS(namespace, "svg");
    const [width, height] = drawing.space || [1, 1];
    preview.setAttribute("viewBox", `0 0 ${width} ${height}`);
    preview.setAttribute("aria-hidden", "true");
    preview.setAttribute("focusable", "false");
    for (const stroke of drawing.strokes) {
      const path = document.createElementNS(namespace, "path");
      let points = stroke
        .map(
          ([x, y], point) =>
            `${point ? "L" : "M"}${(x * width).toFixed(4)} ${(y * height).toFixed(4)}`,
        )
        .join(" ");
      if (stroke.length === 1) points += " l 0.001 0";
      path.setAttribute("d", points);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linecap", "round");
      path.setAttribute("stroke-linejoin", "round");
      path.setAttribute("vector-effect", "non-scaling-stroke");
      preview.append(path);
    }
    button.append(preview);
    return button;
  }

  function completedDrawings() {
    return board.characters
      .map((drawing, index) => ({ drawing, index }))
      .filter(
        ({ drawing, index }) =>
          drawing.retained && (drawing.strokes.length || index === board.index),
      );
  }

  function renderDrawingArchive() {
    const thumbnails = document.querySelector("[data-all-drawings]");
    const active = document.activeElement;
    const selected = thumbnails.contains(active)
      ? active.closest("[data-drawing-select]")?.dataset.drawingSelect
      : undefined;
    thumbnails.replaceChildren(
      ...completedDrawings().map(({ drawing, index }) =>
        drawingPreview(drawing, index),
      ),
    );
    if (selected !== undefined) {
      const target =
        thumbnails.querySelector(`[data-drawing-select="${selected}"]`) ||
        document.querySelector("[data-history-close]");
      target.focus({ preventScroll: true });
    }
  }

  function renderDrawingHistory(selection = false) {
    if (!board) return;
    if (document.querySelector("[data-drawing-archive]").open)
      renderDrawingArchive();
    const history = document.querySelector("[data-drawing-history]");
    if (!board.canvas.getBoundingClientRect().width) return;
    history.style.removeProperty("--history-width");
    const drawings = completedDrawings();
    history.hidden = !drawings.length;
    const thumbnails = history.querySelector("[data-drawing-thumbnails]");
    const active = document.activeElement;
    const focused = thumbnails.contains(active)
      ? active.closest("[data-drawing-select]")?.dataset.drawingSelect
      : undefined;
    const restoreFocus = () => {
      if (focused === undefined) return;
      const target =
        (!history.hidden &&
          (thumbnails.querySelector(`[data-drawing-select="${focused}"]`) ||
            history.querySelector("[data-history-expand]:not([hidden])"))) ||
        board.canvas;
      target.focus({ preventScroll: true });
    };
    if (!drawings.length) {
      thumbnails.replaceChildren();
      restoreFocus();
      return;
    }
    const mobile = matchMedia(
      "(max-width: 700px), (max-width: 1000px) and (max-height: 500px)",
    ).matches;
    const front = !board.canvas
      .closest(".study-card")
      .classList.contains("revealed");
    const compact =
      mobile &&
      front &&
      board.canvas.closest(".study-body").clientHeight <= 320;
    history.dataset.compact = String(compact);
    if (compact) {
      thumbnails.replaceChildren();
      history.querySelector("[data-history-expand]").hidden = false;
      restoreFocus();
      return;
    }
    const probe = drawingPreview(drawings[0].drawing, drawings[0].index);
    probe.style.position = "absolute";
    probe.style.visibility = "hidden";
    thumbnails.append(probe);
    const size = probe.getBoundingClientRect().width;
    const rows = Number(
      getComputedStyle(history).getPropertyValue("--history-rows"),
    );
    const width = mobile
      ? board.canvas.closest(".drawing-stage").getBoundingClientRect().width
      : Math.min(
          thumbnails.getBoundingClientRect().width,
          board.canvas.closest(".study-body").getBoundingClientRect().width /
            2 -
            12,
        );
    const columns = Math.max(1, Math.floor((width + 8) / (size + 8)));
    const capacity = columns * rows;
    const selected = drawings.findIndex(({ index }) => index >= board.index);
    const position = selected === -1 ? drawings.length - 1 : selected;
    let start = Math.max(
      0,
      drawings.findIndex(({ index }) => index >= (board.historyStart ?? 0)),
    );
    if (position < start) start = position;
    if (position >= start + capacity) start = position - capacity + 1;
    start = Math.max(0, Math.min(start, drawings.length - capacity));
    if (selection && capacity > 1) {
      if (position === start && start > 0) start -= 1;
      else if (
        position === start + capacity - 1 &&
        start + capacity < drawings.length
      )
        start += 1;
    }
    board.historyStart = drawings[start].index;
    const visible = drawings.slice(start, start + capacity);
    const usedColumns = Math.min(columns, visible.length);
    history.style.setProperty(
      "--history-width",
      `${usedColumns * size + (usedColumns - 1) * 8}px`,
    );
    const firstRow = visible.length % columns || columns;
    const previewRows = [];
    for (let offset = 0; offset < visible.length;) {
      const count = offset === 0 ? firstRow : columns;
      const row = document.createElement("div");
      row.className = "drawing-thumbnail-row";
      row.append(
        ...visible
          .slice(offset, offset + count)
          .map(({ drawing, index }) => drawingPreview(drawing, index)),
      );
      previewRows.push(row);
      offset += count;
    }
    history.dataset.rows = String(previewRows.length);
    thumbnails.replaceChildren(...previewRows);
    history.querySelector("[data-history-expand]").hidden =
      drawings.length <= capacity;
    restoreFocus();
  }

  function selectDrawing(index) {
    if (index === null) return;
    if (index === board.index) {
      renderDrawingHistory(true);
      return;
    }
    const current = board.characters[board.index];
    current.retained = Boolean(current.strokes.length);
    board.index = index;
    board.characters[index] ||= { strokes: [], space: null };
    renderBoard();
    renderDrawingHistory(true);
  }

  function adjacentDrawing(direction) {
    for (
      let index = board.index + direction;
      index >= 0 && index < board.characters.length;
      index += direction
    ) {
      if (board.characters[index].strokes.length) return index;
    }
    if (direction < 0) return null;
    const last = board.characters.length - 1;
    if (board.index < last && !board.characters[last].strokes.length)
      return last;
    const drawing = board.characters[board.index];
    return drawing.strokes.length || drawing.retained
      ? board.characters.length
      : null;
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
      adjacentDrawing(-1) === null;
    container.querySelector("[data-drawing-next]").disabled =
      adjacentDrawing(1) === null;
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
    const appendPoint = (event) => {
      const stroke = board.characters[board.index].strokes.at(-1);
      const next = point(event);
      const previous = stroke.at(-1);
      if (next[0] !== previous[0] || next[1] !== previous[1]) stroke.push(next);
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
      event.getCoalescedEvents?.().forEach(appendPoint);
      appendPoint(event);
      renderBoard();
    });
    const endStroke = (event) => {
      if (board?.canvas !== canvas || board.pointer !== event.pointerId) return;
      if (event.type === "pointerup") {
        appendPoint(event);
        renderBoard();
      }
      board.pointer = null;
      if (canvas.hasPointerCapture(event.pointerId))
        canvas.releasePointerCapture(event.pointerId);
      renderDrawingHistory();
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
    if (
      document.querySelector("#study-session")?.dataset.direction ===
      "listening"
    )
      playAudio();
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
    syncFlags();
    syncAudio();
    playAudio();
    schedulePronunciation();
    requestAnimationFrame(() => {
      renderBoard();
      renderDrawingHistory();
    });
  });
  document.fonts.addEventListener("loadingdone", schedulePronunciation);
  document.fonts.ready.then(schedulePronunciation);
  document.addEventListener("yiju:pronunciation-added", mountPronunciation);

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
      document.dispatchEvent(
        new CustomEvent("yiju:audio-changed", { detail: { muted } }),
      );
    }
    if (button.matches("[data-audio-replay], [data-listening-play]")) {
      if (player?.error) stopAudio();
      if (button.dataset.audioFailed) {
        stopAudio();
        delete button.dataset.audioFailed;
      }
      playAudio();
    }
    if (
      button.matches(
        "[data-drawing-undo], [data-drawing-clear], [data-drawing-next], [data-drawing-previous], [data-drawing-select]",
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
      renderDrawingHistory();
    }
    if (button.matches("[data-drawing-clear]") && board) {
      const drawing = board.characters[board.index];
      drawing.strokes = [];
      drawing.space = null;
      renderBoard();
      renderDrawingHistory();
    }
    if (button.matches("[data-drawing-next]") && board) {
      selectDrawing(adjacentDrawing(1));
    }
    if (button.matches("[data-drawing-previous]") && board) {
      selectDrawing(adjacentDrawing(-1));
    }
    if (button.matches("[data-drawing-select]") && board) {
      const index = Number(button.dataset.drawingSelect);
      if (!Number.isInteger(index) || !board.characters[index]) return;
      document.querySelector("[data-drawing-archive]").close();
      selectDrawing(index);
      board.canvas.focus({ preventScroll: true });
    }
    if (button.matches("[data-history-expand]") && board) {
      renderDrawingArchive();
      document.querySelector("[data-drawing-archive]").showModal();
    }
    if (button.matches("[data-history-close]")) {
      button.closest("dialog").close();
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
