const fileInput = document.querySelector("#profile-image-file");
const dataInput = document.querySelector("#profile-image-data");
const preview = document.querySelector(".profile-preview");
const statusText = document.querySelector("#profile-image-status");

const setStatus = (message, isError = false) => {
  statusText.textContent = message;
  statusText.classList.toggle("form-error", isError);
};

const loadImage = (file) => new Promise((resolve, reject) => {
  const image = new Image();
  const objectUrl = URL.createObjectURL(file);
  image.onload = () => {
    URL.revokeObjectURL(objectUrl);
    resolve(image);
  };
  image.onerror = () => {
    URL.revokeObjectURL(objectUrl);
    reject(new Error("사진을 읽을 수 없습니다."));
  };
  image.src = objectUrl;
});

fileInput?.addEventListener("change", async () => {
  const file = fileInput.files?.[0];
  dataInput.value = "";
  if (!file) return;
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    fileInput.value = "";
    return setStatus("JPG, PNG, WebP 사진만 선택할 수 있습니다.", true);
  }
  if (file.size > 5 * 1024 * 1024) {
    fileInput.value = "";
    return setStatus("원본 사진은 5MB 이하여야 합니다.", true);
  }

  try {
    setStatus("사진을 다듬는 중입니다...");
    const image = await loadImage(file);
    const sourceSize = Math.min(image.naturalWidth, image.naturalHeight);
    const outputSize = Math.min(512, sourceSize);
    const sourceX = Math.floor((image.naturalWidth - sourceSize) / 2);
    const sourceY = Math.floor((image.naturalHeight - sourceSize) / 2);
    const canvas = document.createElement("canvas");
    canvas.width = outputSize;
    canvas.height = outputSize;
    const context = canvas.getContext("2d");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, outputSize, outputSize);
    context.drawImage(image, sourceX, sourceY, sourceSize, sourceSize, 0, 0, outputSize, outputSize);
    const dataUrl = canvas.toDataURL("image/jpeg", 0.84);
    if (dataUrl.length > 680000) throw new Error("축소한 사진도 용량이 큽니다. 다른 사진을 선택해주세요.");
    dataInput.value = dataUrl;
    if (preview?.tagName === "IMG") {
      preview.src = dataUrl;
    } else if (preview) {
      const imagePreview = document.createElement("img");
      imagePreview.className = preview.className.replace("profile-avatar-fallback", "");
      imagePreview.alt = "새 프로필 사진 미리보기";
      imagePreview.src = dataUrl;
      preview.replaceWith(imagePreview);
    }
    setStatus("새 프로필 사진을 선택했습니다. 저장 버튼을 눌러 적용하세요.");
  } catch (error) {
    fileInput.value = "";
    setStatus(error.message || "사진을 처리하지 못했습니다.", true);
  }
});
