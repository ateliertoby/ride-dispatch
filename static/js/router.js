// The views share one document. The path says which is showing; switching
// hides one and shows the other and never asks the server for a page.
//
// views is { name: { path, title, view, root, deps } }, where view is
// { mount(root, deps), show(), hide(), refresh() }. A view is mounted the
// first time it is shown and stays mounted.
export function createRouter(views, { onSwitch } = {}) {
  const byPath = new Map(Object.entries(views).map(([name, v]) => [v.path, name]));
  const scroll = {};
  const mounted = new Set();
  let current = null;

  // A path no view claims shows the view at '/'.
  function nameFor(pathname) { return byPath.get(pathname) || byPath.get('/'); }

  function show(name) {
    if (name === current) return;
    if (current) {
      scroll[current] = window.scrollY;
      views[current].view.hide();
      views[current].root.hidden = true;
    }
    const v = views[name];
    current = name;
    document.body.dataset.view = name;
    document.title = v.title;
    v.root.hidden = false;
    if (!mounted.has(name)) { mounted.add(name); v.view.mount(v.root, v.deps); }
    // The views scroll the document, so each one's position is put back
    // before it paints anything that reads geometry.
    window.scrollTo(0, scroll[name] || 0);
    if (onSwitch) onSwitch(name);
    v.view.show();
  }

  function go(path) {
    const name = nameFor(path);
    if (name === current) return;
    history.pushState({}, '', views[name].path);
    show(name);
  }

  function start() {
    history.scrollRestoration = 'manual';
    document.addEventListener('click', e => {
      const a = e.target.closest('a[data-nav]');
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
      const path = new URL(a.href).pathname;
      // A link to a path no view claims is left to the browser, which asks
      // the server for it.
      if (!byPath.has(path)) return;
      e.preventDefault();
      go(path);
    });
    window.addEventListener('popstate', () => show(nameFor(location.pathname)));
    show(nameFor(location.pathname));
  }

  return { start, go, current: () => current };
}
