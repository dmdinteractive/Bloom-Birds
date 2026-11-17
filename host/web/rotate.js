/* Screen rotation for the Pi's touch screen.
 *
 * The Touch Display 2 is a portrait panel (720 wide, 1280 tall). These pages are laid out for
 * landscape, so when the browser window is taller than it is wide the whole page is turned a
 * quarter turn. Touches still land where you tap, because the browser turns them too.
 *
 * If the display is mounted the other way up, the Service view's "Flip screen" button turns it
 * a further half turn. That choice is remembered by this browser only.
 */
"use strict";
(function () {
  const KEY = "bloombirds_rotation";
  function flipped() { try { return localStorage.getItem(KEY) === "flip"; } catch (e) { return false; } }
  function degrees() {
    const base = window.innerHeight > window.innerWidth ? 90 : 0;     // portrait screen: quarter turn
    return (base + (flipped() ? 180 : 0)) % 360;
  }
  function apply() {
    const d = degrees(), h = document.documentElement;
    h.classList.remove("rot90", "rot180", "rot270");
    if (d) h.classList.add("rot" + d);
    return d;
  }
  window.screenRotation = {
    degrees,
    flip() {
      try { flipped() ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, "flip"); } catch (e) { /* not remembered */ }
      return apply();
    },
  };
  apply();
  window.addEventListener("resize", apply);
})();
