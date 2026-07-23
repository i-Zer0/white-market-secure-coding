(() => {
  const button = document.querySelector("#share-product");
  const status = document.querySelector("#share-status");
  if (!button || !status) return;

  const fallbackCopy = (text) => {
    const input = document.createElement("textarea");
    input.value = text;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    const copied = document.execCommand("copy");
    input.remove();
    if (!copied) throw new Error("copy failed");
  };

  button.addEventListener("click", async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(window.location.href);
      } else {
        fallbackCopy(window.location.href);
      }
      status.textContent = "링크가 복사되었습니다.";
    } catch {
      status.textContent = "링크를 복사하지 못했습니다.";
    }
  });
})();

(() => {
  const gallery = document.querySelector("[data-gallery]");
  if (!gallery) return;
  const slides = [...gallery.querySelectorAll(".gallery-slide")];
  const thumbnails = [...gallery.querySelectorAll("[data-gallery-index]")];
  const currentLabel = gallery.querySelector("[data-gallery-current]");
  if (slides.length < 2) return;
  let current = 0;

  const show = (index) => {
    current = (index + slides.length) % slides.length;
    slides.forEach((slide, slideIndex) => { slide.hidden = slideIndex !== current; });
    thumbnails.forEach((thumbnail, thumbnailIndex) => {
      thumbnail.classList.toggle("selected", thumbnailIndex === current);
      thumbnail.setAttribute("aria-current", thumbnailIndex === current ? "true" : "false");
    });
    if (currentLabel) currentLabel.textContent = String(current + 1);
  };

  gallery.querySelector("[data-gallery-prev]")?.addEventListener("click", () => show(current - 1));
  gallery.querySelector("[data-gallery-next]")?.addEventListener("click", () => show(current + 1));
  thumbnails.forEach((thumbnail) => {
    thumbnail.addEventListener("click", () => show(Number(thumbnail.dataset.galleryIndex)));
  });
  gallery.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") show(current - 1);
    if (event.key === "ArrowRight") show(current + 1);
  });
  show(0);
})();
