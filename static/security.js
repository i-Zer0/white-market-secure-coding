(() => {
  document.querySelectorAll("[data-copy-recovery]").forEach((button) => {
    button.addEventListener("click", async () => {
      const codes = [...document.querySelectorAll(".recovery-codes code")]
        .map((node) => node.textContent.trim())
        .filter(Boolean)
        .join("\n");
      await navigator.clipboard.writeText(codes);
      button.textContent = "복사 완료";
    });
  });

  document.querySelectorAll("[data-auto-submit]").forEach((control) => {
    control.addEventListener("change", () => control.form.submit());
  });
})();
