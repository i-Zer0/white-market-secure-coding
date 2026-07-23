(() => {
  const form = document.querySelector("#register-form");
  const username = document.querySelector("#username");
  const checkButton = document.querySelector("#username-check");
  const status = document.querySelector("#username-status");
  const location = document.querySelector("#location");
  const locationButton = document.querySelector("#location-detect");
  const locationStatus = document.querySelector("#location-status");

  if (!form || !username || !checkButton || !status) return;

  let checkedUsername = "";

  const setStatus = (message, state) => {
    status.textContent = message;
    status.className = `field-status ${state}`;
  };

  const setLocationStatus = (message, state = "") => {
    locationStatus.textContent = message;
    locationStatus.className = `field-status ${state}`;
  };

  const permissionDeniedMessage =
    "위치 권한이 차단되어 있습니다. 주소창 왼쪽의 사이트 설정에서 위치를 허용한 뒤 새로고침하거나 동네를 직접 입력해 주세요.";

  const requestCurrentLocation = () => {
    locationButton.disabled = true;
    setLocationStatus("현재 위치를 확인하는 중...");
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        try {
          const params = new URLSearchParams({
            lat: String(position.coords.latitude),
            lon: String(position.coords.longitude),
          });
          const response = await fetch(`/api/location/reverse?${params}`, {
            headers: { Accept: "application/json" },
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || "동네를 찾지 못했습니다.");
          location.value = result.district;
          setLocationStatus(`${result.district}(으)로 입력했습니다.`, "success");
        } catch (error) {
          setLocationStatus(error.message || "동네를 찾지 못했습니다. 직접 입력해 주세요.", "error");
        } finally {
          locationButton.disabled = false;
        }
      },
      (error) => {
        const messages = {
          1: permissionDeniedMessage,
          2: "현재 위치를 확인할 수 없습니다. Windows 위치 서비스가 켜져 있는지 확인하거나 동네를 직접 입력해 주세요.",
          3: "위치 확인 시간이 초과되었습니다. 다시 시도해 주세요.",
        };
        setLocationStatus(messages[error.code] || "현재 위치를 사용할 수 없습니다.", "error");
        locationButton.disabled = false;
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
    );
  };

  username.addEventListener("input", () => {
    checkedUsername = "";
    setStatus("", "");
  });

  checkButton.addEventListener("click", async () => {
    const value = username.value.trim();
    if (!value) {
      setStatus("아이디를 먼저 입력하세요.", "error");
      username.focus();
      return;
    }

    checkButton.disabled = true;
    setStatus("확인 중...", "");
    try {
      const response = await fetch(`/api/username-check?username=${encodeURIComponent(value)}`, {
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("request failed");
      const result = await response.json();
      checkedUsername = result.available ? value : "";
      setStatus(result.message, result.available ? "success" : "error");
    } catch (_error) {
      checkedUsername = "";
      setStatus("중복확인을 완료하지 못했습니다.", "error");
    } finally {
      checkButton.disabled = false;
    }
  });

  form.addEventListener("submit", (event) => {
    if (checkedUsername !== username.value.trim()) {
      event.preventDefault();
      setStatus("아이디 중복확인을 진행하세요.", "error");
      username.focus();
    }
  });

  if (location && locationButton && locationStatus) {
    locationButton.addEventListener("click", async () => {
      if (!window.isSecureContext || !navigator.geolocation) {
        setLocationStatus("이 브라우저에서는 현재 위치를 사용할 수 없습니다. 직접 입력해 주세요.", "error");
        return;
      }

      if (navigator.permissions?.query) {
        try {
          const permission = await navigator.permissions.query({ name: "geolocation" });
          if (permission.state === "denied") {
            setLocationStatus(permissionDeniedMessage, "error");
            return;
          }
        } catch (_error) {
          // Some browsers expose geolocation without supporting permission queries.
        }
      }

      requestCurrentLocation();
    });
  }
})();
