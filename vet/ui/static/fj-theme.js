// fjkit's pre-paint theme script, moved out of the page: this app's CSP runs script files only.
// Sets html.dark before the first paint, from a saved choice or the system setting.
(() => {
  let saved = null;
  try { saved = localStorage.getItem("theme"); } catch (e) { saved = null; }
  const dark = saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.classList.toggle("dark", dark);
})();
