// fjkit's toast listener, moved out of the page: this app's CSP runs script files only.
document.body.addEventListener("fjkit:toast", (event) => {
  const toaster = document.getElementById("toaster");
  if (!toaster || !toaster.toast) return;
  for (const message of event.detail?.messages ?? []) toaster.toast(message);
});
