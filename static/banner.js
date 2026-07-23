(() => {
  const banner = document.querySelector(".education-banner");
  if (!banner) return;

  const slides = [...banner.querySelectorAll("[data-ad-slide]")];
  const current = banner.querySelector("[data-ad-current]");
  let index = 0;
  let timer;

  const show = (nextIndex) => {
    index = (nextIndex + slides.length) % slides.length;
    slides.forEach((slide, slideIndex) => {
      slide.hidden = slideIndex !== index;
    });
    current.textContent = String(index + 1);
  };

  const start = () => {
    window.clearInterval(timer);
    timer = window.setInterval(() => show(index + 1), 6000);
  };

  banner.querySelector("[data-ad-prev]").addEventListener("click", () => {
    show(index - 1);
    start();
  });
  banner.querySelector("[data-ad-next]").addEventListener("click", () => {
    show(index + 1);
    start();
  });
  banner.addEventListener("mouseenter", () => window.clearInterval(timer));
  banner.addEventListener("mouseleave", start);
  banner.addEventListener("focusin", () => window.clearInterval(timer));
  banner.addEventListener("focusout", start);
  start();
})();
