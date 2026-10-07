document.addEventListener("keydown", (event) => {
  if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
  if (
    event.target.closest(
      "input, textarea, select, button, a, summary, [contenteditable]",
    )
  )
    return;
  const reveal = document.querySelector("#reveal-answer");
  if (event.code === "Space" && reveal) {
    event.preventDefault();
    reveal.click();
    return;
  }
  if (/^[1-4]$/.test(event.key)) {
    const button = document.querySelector(`[data-rating="${event.key}"]`);
    if (button) {
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
