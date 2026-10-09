(() => {
  let installPrompt;
  let installed = false;
  let prompting = false;
  const standalone = window.matchMedia("(display-mode: standalone)");
  const ios =
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const androidFirefox =
    /Android/.test(navigator.userAgent) &&
    /Firefox\//.test(navigator.userAgent);

  function update() {
    const panel = document.querySelector("[data-install-panel]");
    if (!panel) return;
    const active = installed || standalone.matches || navigator.standalone;
    panel.hidden = active || (!installPrompt && !ios && !androidFirefox);
    const button = panel.querySelector("[data-install-button]");
    button.hidden = !installPrompt;
    button.disabled = prompting;
    panel.querySelector("[data-install-ios]").hidden = !ios || !!installPrompt;
    panel.querySelector("[data-install-firefox]").hidden =
      !androidFirefox || !!installPrompt;
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    update();
  });
  window.addEventListener("appinstalled", () => {
    installed = true;
    installPrompt = null;
    update();
  });
  standalone.addEventListener("change", update);
  window.addEventListener("pageshow", update);

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-install-button]");
    if (!button || !installPrompt || prompting) return;
    const prompt = installPrompt;
    const status = button
      .closest("[data-install-panel]")
      .querySelector("[data-install-status]");
    status.hidden = true;
    prompting = true;
    update();
    try {
      await prompt.prompt();
      await prompt.userChoice;
      installPrompt = null;
    } catch {
      status.textContent = "Could not open the install prompt. Try again.";
      status.hidden = false;
    } finally {
      prompting = false;
      update();
    }
  });

  new MutationObserver(update).observe(document.body, { childList: true });
  update();
})();
