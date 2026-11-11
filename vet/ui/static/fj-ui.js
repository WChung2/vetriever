// fjkit's sidebar button and theme switch, without their inline onclick: this app's CSP runs
// script files only. One listener for both.
document.addEventListener("click", (event) => {
  const side = event.target.closest("[data-sidebar-toggle]");
  if (side) {
    const sidebar = document.getElementById(side.getAttribute("aria-controls"));
    if (sidebar && sidebar.toggle) sidebar.toggle();
    return;
  }
  if (event.target.closest("[data-theme-toggle]")) {
    const dark = document.documentElement.classList.toggle("dark");
    try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) { /* private mode */ }
  }
});
