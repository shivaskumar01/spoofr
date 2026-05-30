"use strict";

/* ---- mobile nav ---- */
const navToggle = document.getElementById("navToggle");
const navLinks = document.getElementById("navLinks");
if (navToggle && navLinks) {
  navToggle.addEventListener("click", () => navLinks.classList.toggle("open"));
  navLinks.querySelectorAll("a").forEach(a => a.addEventListener("click", () => navLinks.classList.remove("open")));
}

const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---- Lenis: buttery momentum scroll ---- */
let lenis = null;
try {
  if (window.Lenis && !reduce) {
    lenis = new Lenis({ lerp: 0.09, wheelMultiplier: 1.0 });
    const raf = (t) => { lenis.raf(t); requestAnimationFrame(raf); };
    requestAnimationFrame(raf);
    document.querySelectorAll('a[href^="#"]').forEach(a => {
      a.addEventListener("click", (e) => {
        const el = document.querySelector(a.getAttribute("href"));
        if (el) { e.preventDefault(); lenis.scrollTo(el, { offset: -40 }); }
      });
    });
  }
} catch (e) {}

/* ---- scroll reveals: robust IntersectionObserver ---- */
try {
  const io = new IntersectionObserver((entries) => {
    entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
  }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
  document.querySelectorAll(".reveal").forEach(el => io.observe(el));
} catch (e) {
  document.querySelectorAll(".reveal").forEach(el => el.classList.add("in"));
}
// safety net — never leave anything hidden if observers misbehave
setTimeout(() => document.querySelectorAll(".reveal:not(.in)").forEach(el => {
  if (el.getBoundingClientRect().top < innerHeight) el.classList.add("in");
}), 1600);

/* ---- GSAP hero intro (one-shot, no scroll dependency) ---- */
try { if (window.gsap && !reduce) gsap.from(".hero-inner > *", { y: 26, opacity: 0, duration: 0.85, stagger: 0.08, ease: "power3.out", delay: 0.15 }); } catch (e) {}

/* ---- globe.gl: dark earth, blue atmosphere, animated arcs + pulsing rings ---- */
try {
  const el = document.getElementById("globe");
  if (el && window.Globe && !reduce) {
    const cities = [
      [40.71,-74.0],[51.50,-0.12],[35.67,139.65],[48.85,2.35],[-23.55,-46.63],
      [1.35,103.8],[-33.86,151.2],[55.75,37.61],[19.07,72.87],[37.77,-122.4],
      [-1.29,36.82],[30.04,31.23],[64.14,-21.94],[-34.6,-58.38],[25.20,55.27]
    ];
    const pick = () => cities[Math.floor(Math.random() * cities.length)];
    const arcs = Array.from({ length: 16 }, () => {
      const a = pick(), b = pick();
      return { startLat: a[0], startLng: a[1], endLat: b[0], endLng: b[1] };
    });
    const pts = cities.map(c => ({ lat: c[0], lng: c[1] }));

    const g = Globe()(el)
      .globeImageUrl("//unpkg.com/three-globe/example/img/earth-dark.jpg")
      .bumpImageUrl("//unpkg.com/three-globe/example/img/earth-topology.png")
      .backgroundColor("rgba(0,0,0,0)")
      .showGraticules(true)
      .atmosphereColor("#3b82f6").atmosphereAltitude(0.24)
      .arcsData(arcs)
        .arcColor(() => ["rgba(59,130,246,0.05)", "rgba(34,211,238,0.95)"])
        .arcStroke(0.45).arcDashLength(0.5).arcDashGap(0.22)
        .arcDashAnimateTime(() => 1500 + Math.random() * 1400)
        .arcAltitudeAutoScale(0.45)
      .ringsData(pts)
        .ringColor(() => (t) => `rgba(34,211,238,${1 - t})`)
        .ringMaxRadius(4).ringPropagationSpeed(2.2).ringRepeatPeriod(1500)
      .pointsData(pts).pointColor(() => "#9bd6ff").pointAltitude(0.004).pointRadius(0.2);

    const size = () => g.width(el.clientWidth).height(el.clientHeight);
    size(); window.addEventListener("resize", size);

    const c = g.controls();
    c.enableZoom = false; c.autoRotate = true; c.autoRotateSpeed = 0.55;
    g.pointOfView({ lat: 16, lng: -40, altitude: 2.4 });
  }
} catch (e) {}
