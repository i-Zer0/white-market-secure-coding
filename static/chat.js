(() => {
  const list = document.querySelector(".messages[data-peer-id]");
  const form = document.querySelector("form[data-live-chat]");
  if (!list || !form) return;

  const currentUser = Number(list.dataset.currentUser);
  const peerId = Number(list.dataset.peerId);
  const productId = Number(list.dataset.productId);
  let lastId = Number(list.dataset.lastId || 0);
  let imageData = "";
  let stream;

  const escapeHtml = (value) => {
    const node = document.createElement("span");
    node.textContent = value || "";
    return node.innerHTML;
  };

  const renderMessage = (message) => {
    if (list.querySelector(`[data-message-id="${message.id}"]`)) return;
    list.querySelector(".chat-day-divider")?.remove();
    const mine = Number(message.sender_id) === currentUser;
    const item = document.createElement("li");
    item.className = `chat-message ${mine ? "mine" : "theirs"}`;
    item.dataset.messageId = message.id;
    const avatar = mine
      ? ""
      : message.sender_profile_image_url
        ? `<img class="profile-avatar small" src="${escapeHtml(message.sender_profile_image_url)}" alt="">`
        : `<span class="profile-avatar small profile-avatar-fallback">${escapeHtml((message.sender_name || "?").slice(0, 1))}</span>`;
    const image = message.image_url
      ? `<img class="chat-image" src="${escapeHtml(message.image_url)}" alt="채팅으로 보낸 사진">`
      : "";
    const body = message.body ? `<p>${escapeHtml(message.body)}</p>` : "";
    item.innerHTML = `${avatar}<div class="message-bubble">${image}${body}<span class="message-meta"><span class="read-state">${mine && message.is_read ? "읽음" : ""}</span><time>${escapeHtml(message.created_at)}</time></span></div>`;
    list.append(item);
    lastId = Math.max(lastId, Number(message.id));
    list.scrollTop = list.scrollHeight;
  };

  const connect = () => {
    stream?.close();
    stream = new EventSource(`/chat/stream?user=${peerId}&product=${productId}&after=${lastId}`);
    stream.onmessage = (event) => {
      const data = JSON.parse(event.data);
      data.messages.forEach(renderMessage);
      document.querySelector("[data-presence]").textContent = data.online ? "접속 중" : "오프라인";
      document.querySelectorAll(".chat-message.mine").forEach((item) => {
        if (Number(item.dataset.messageId) <= Number(data.read_through)) {
          item.querySelector(".read-state").textContent = "읽음";
        }
      });
    };
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = form.querySelector("textarea").value.trim();
    if (!body && !imageData) return;
    const submit = form.querySelector("button[type='submit'], button.primary");
    const originalLabel = submit.textContent;
    submit.disabled = true;
    submit.textContent = "전송 중";
    form.setAttribute("aria-busy", "true");
    form.querySelector("input[name='image_data']").value = imageData;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        redirect: "follow",
      });
      if (!response.ok) throw new Error("메시지를 보내지 못했습니다.");
      form.querySelector("textarea").value = "";
      form.querySelector("input[name='image_data']").value = "";
      form.querySelector("[data-chat-image]").value = "";
      form.querySelector(".chat-image-preview").hidden = true;
      form.querySelector(".chat-image-preview").innerHTML = "";
      imageData = "";
    } catch (error) {
      window.alert(error.message);
    } finally {
      submit.disabled = false;
      submit.textContent = originalLabel;
      form.removeAttribute("aria-busy");
      form.querySelector("textarea").focus();
    }
  });

  form.querySelector("[data-chat-image]").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    imageData = canvas.toDataURL("image/jpeg", 0.82);
    if (imageData.length > 1_200_000) {
      imageData = "";
      event.target.value = "";
      window.alert("사진 용량을 줄여 다시 선택해주세요.");
      return;
    }
    const preview = form.querySelector(".chat-image-preview");
    preview.innerHTML = `<img src="${imageData}" alt="전송할 사진 미리보기"><button type="button" aria-label="사진 제거">×</button>`;
    preview.hidden = false;
    preview.querySelector("button").addEventListener("click", () => {
      imageData = "";
      event.target.value = "";
      preview.hidden = true;
      preview.innerHTML = "";
    });
  });

  window.addEventListener("beforeunload", () => stream?.close());
  connect();
  list.scrollTop = list.scrollHeight;
})();
