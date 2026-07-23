(() => {
  const form = document.querySelector("#product-form");
  const fileInput = document.querySelector("#product-image-files");
  const list = document.querySelector("#product-image-list");
  const manifestInput = document.querySelector("#image-manifest");
  const status = document.querySelector("#product-image-status");
  const counter = document.querySelector("#product-image-count");
  if (!form || !fileInput || !list || !manifestInput) return;

  let serial = 0;
  let items = [...list.querySelectorAll(".product-image-item")].map((element) => ({
    key: `existing-${element.dataset.imageId}`,
    kind: "existing",
    id: Number(element.dataset.imageId),
    src: element.dataset.imageUrl,
    primary: element.dataset.primary === "1",
  }));

  const setStatus = (message, error = false) => {
    status.textContent = message;
    status.classList.toggle("error", error);
  };

  const syncManifest = () => {
    if (items.length && !items.some((item) => item.primary)) items[0].primary = true;
    manifestInput.value = JSON.stringify(items.map((item) => ({
      kind: item.kind,
      id: item.id,
      data: item.data,
      thumbnail: item.thumbnail,
      primary: item.primary,
    })));
    counter.textContent = `${items.length} / 8`;
  };

  const move = (index, direction) => {
    const destination = index + direction;
    if (destination < 0 || destination >= items.length) return;
    [items[index], items[destination]] = [items[destination], items[index]];
    render();
  };

  const render = () => {
    list.replaceChildren();
    items.forEach((item, index) => {
      const row = document.createElement("li");
      row.className = "product-image-item";

      const image = document.createElement("img");
      image.src = item.src;
      image.alt = `${index + 1}번째 상품 사진`;

      const copy = document.createElement("div");
      copy.className = "product-image-copy";
      const radioLabel = document.createElement("label");
      radioLabel.className = "primary-image-choice";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "primary-image-choice";
      radio.checked = item.primary;
      radio.addEventListener("change", () => {
        items.forEach((candidate) => { candidate.primary = candidate.key === item.key; });
        render();
      });
      radioLabel.append(radio, document.createTextNode(" 대표 사진"));
      const order = document.createElement("small");
      order.textContent = `${index + 1}번째 사진`;
      copy.append(radioLabel, order);

      const controls = document.createElement("div");
      controls.className = "image-order-controls";
      const up = document.createElement("button");
      up.type = "button";
      up.textContent = "↑";
      up.title = "앞으로 이동";
      up.setAttribute("aria-label", `${index + 1}번째 사진 앞으로 이동`);
      up.disabled = index === 0;
      up.addEventListener("click", () => move(index, -1));
      const down = document.createElement("button");
      down.type = "button";
      down.textContent = "↓";
      down.title = "뒤로 이동";
      down.setAttribute("aria-label", `${index + 1}번째 사진 뒤로 이동`);
      down.disabled = index === items.length - 1;
      down.addEventListener("click", () => move(index, 1));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.title = "사진 삭제";
      remove.setAttribute("aria-label", `${index + 1}번째 사진 삭제`);
      remove.className = "danger";
      remove.addEventListener("click", () => {
        items.splice(index, 1);
        render();
      });
      controls.append(up, down, remove);
      row.append(image, copy, controls);
      list.append(row);
    });
    syncManifest();
  };

  const loadImage = (file) => new Promise((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(file);
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("사진을 읽을 수 없습니다."));
    };
    image.src = url;
  });

  const prepareImage = async (file) => {
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      throw new Error("JPG, PNG, WebP 사진만 선택할 수 있습니다.");
    }
    if (file.size > 8 * 1024 * 1024) throw new Error("원본 사진은 한 장당 8MB 이하여야 합니다.");
    const image = await loadImage(file);
    const scale = Math.min(1, 1200 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext("2d");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    let data = canvas.toDataURL("image/jpeg", 0.8);
    if (data.length > 1_200_000) data = canvas.toDataURL("image/jpeg", 0.64);
    if (data.length > 1_200_000) throw new Error("축소한 사진의 용량이 큽니다. 다른 사진을 선택해주세요.");
    const thumbnailCanvas = document.createElement("canvas");
    const thumbnailScale = Math.min(1, 320 / Math.max(image.naturalWidth, image.naturalHeight));
    thumbnailCanvas.width = Math.max(1, Math.round(image.naturalWidth * thumbnailScale));
    thumbnailCanvas.height = Math.max(1, Math.round(image.naturalHeight * thumbnailScale));
    const thumbnailContext = thumbnailCanvas.getContext("2d");
    thumbnailContext.fillStyle = "#ffffff";
    thumbnailContext.fillRect(0, 0, thumbnailCanvas.width, thumbnailCanvas.height);
    thumbnailContext.drawImage(image, 0, 0, thumbnailCanvas.width, thumbnailCanvas.height);
    return { data, thumbnail: thumbnailCanvas.toDataURL("image/jpeg", 0.76) };
  };

  fileInput.addEventListener("change", async () => {
    const selected = [...(fileInput.files || [])];
    fileInput.value = "";
    if (!selected.length) return;
    if (items.length + selected.length > 8) {
      return setStatus("상품 사진은 최대 8장까지 등록할 수 있습니다.", true);
    }
    setStatus("사진을 안전하게 변환하는 중입니다...");
    try {
      for (const file of selected) {
        const prepared = await prepareImage(file);
        items.push({
          key: `new-${serial++}`,
          kind: "new",
          data: prepared.data,
          thumbnail: prepared.thumbnail,
          src: prepared.data,
          primary: items.length === 0,
        });
      }
      render();
      setStatus("사진을 추가했습니다. 대표 사진과 순서를 확인해주세요.");
    } catch (error) {
      setStatus(error.message || "사진을 처리하지 못했습니다.", true);
    }
  });

  form.addEventListener("submit", syncManifest);
  render();
})();
