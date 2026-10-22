// Light and dark follow the device's prefers-color-scheme, with no switch and nothing stored:
// this puts shadcn's `.dark` class on <html> before the first paint, so a page never flashes the other scheme while
// React loads, and moves it when the device changes scheme
// (https://developer.mozilla.org/en-US/docs/Web/API/MediaQueryList/change_event). A classic blocking script from this
// origin, because the CSP allows no inline script (backend payments/pages). The browser chrome (the theme-color and
// color-scheme meta tags in index.html) follows the same media query by itself.
;(function followSystemColorScheme() {
  var darkSchemeQuery = window.matchMedia("(prefers-color-scheme: dark)")
  function applyColorScheme() {
    var rootElement = document.documentElement
    rootElement.classList.toggle("dark", darkSchemeQuery.matches)
    rootElement.classList.toggle("light", !darkSchemeQuery.matches)
  }
  applyColorScheme()
  darkSchemeQuery.addEventListener("change", applyColorScheme)
})()
