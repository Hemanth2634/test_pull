import { PhoneCamera } from "./camera.js";

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function correctionPayload(value) {
  if (value === "" || value === null || value === undefined) return {};
  const distance = Number(typeof value === "string" ? value.trim() : value);
  if (typeof value === "string" && !value.trim()) return {};
  if (!Number.isFinite(distance) || distance <= 0) {
    throw new Error("Enter a measured distance greater than zero, in centimetres.");
  }
  return { distance_cm: distance };
}

export function createApi(fetchImpl = globalThis.fetch) {
  async function request(path, options) {
    let response;
    try {
      response = await fetchImpl(path, options);
    } catch (error) {
      throw new ApiError("The server could not be reached. Check your connection.");
    }
    let data;
    try { data = await response.json(); }
    catch { throw new ApiError("The server returned an unreadable response.", response.status); }
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "The request failed.";
      throw new ApiError(detail, response.status);
    }
    return data;
  }
  return {
    health: () => request("/api/health"),
    config: () => request("/api/config"),
    predict: blob => request("/api/predictions", {
      method: "POST", headers: { "Content-Type": blob.type || "image/jpeg" }, body: blob,
    }),
    confirm: (id, payload) => request(`/api/predictions/${encodeURIComponent(id)}/confirm`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }),
    cancel: id => request(`/api/predictions/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  };
}

function validObject(object) {
  return object && Number.isFinite(object.distance_cm) && object.distance_cm > 0 &&
    Array.isArray(object.box) && object.box.length === 4 && object.box.every(Number.isFinite);
}

export function commandDescription(result) {
  if (!result || !["MOVE_BACK", "ROTATE", "MOVE_SIDE"].includes(result.command)) {
    return "The server returned an invalid command response.";
  }
  const distance = Number(result.distance_cm).toFixed(2);
  if (result.delivered) return `${result.command} at ${distance} cm — acknowledged by ESP32.`;
  if (result.delivery_error) {
    return `${result.command} at ${distance} cm — delivery failed or is unconfirmed: ${result.delivery_error}. Do not retry this confirmation.`;
  }
  return `${result.command} at ${distance} cm — recorded as a symbolic command; ESP32 delivery is not configured.`;
}

/** One camera frame has one human confirmation. Never retries a confirmation. */
export class DepthWorkflow {
  constructor(api, { now = () => Date.now() / 1000, onChange = () => {} } = {}) {
    this.api = api;
    this.now = now;
    this.onChange = onChange;
    this.phase = "idle";
    this.pending = null;
    this.result = null;
    this.message = "Capture a frame to estimate its nearest depth region.";
    this.generation = 0;
  }

  get canCapture() { return this.phase === "idle"; }
  get canConfirm() {
    return this.phase === "pending" && this.pending &&
      (this.pending.expires_at === null || this.pending.expires_at > this.now());
  }

  changed() { this.onChange(this); }

  async analyze(blob) {
    if (!this.canCapture) throw new Error("Finish or discard the current prediction first.");
    const generation = ++this.generation;
    this.phase = "estimating";
    this.result = null;
    this.message = "Estimating depth. No command has been requested.";
    this.changed();
    try {
      const result = await this.api.predict(blob);
      if (generation !== this.generation) {
        if (result.id) this.api.cancel(result.id).catch(() => {});
        return null;
      }
      if (!Array.isArray(result.objects)) throw new Error("The server returned invalid depth regions.");
      this.result = result;
      if (result.selected === null && result.id === null) {
        this.phase = "idle";
        this.message = "No valid depth region was found. No command was issued; capture another frame.";
      } else {
        if (!result.id || !validObject(result.selected) || !result.objects.every(validObject) ||
            (result.expires_at !== null && !Number.isFinite(result.expires_at))) {
          throw new Error("The server returned an invalid prediction.");
        }
        this.pending = result;
        this.phase = "pending";
        this.message = "Check the nearest-region estimate. Confirm it or enter the measured distance before issuing one command.";
        this.expire();
      }
      this.changed();
      return result;
    } catch (error) {
      if (generation !== this.generation) return null;
      this.pending = null;
      this.phase = "idle";
      this.message = error.status === 503
        ? "No trained depth model is available. Provide the RGB/depth dataset, train the model, and restart the server with its checkpoint."
        : `Estimation failed: ${error.message} No command was issued.`;
      this.changed();
      throw error;
    }
  }

  async confirm(correction = "") {
    if (!this.canConfirm) {
      this.expire();
      throw new Error("There is no unexpired prediction to confirm. Capture another frame.");
    }
    const payload = correctionPayload(correction);
    const prediction = this.pending;
    const generation = ++this.generation;
    this.pending = null;
    this.phase = "confirming";
    this.message = "Submitting one human-confirmed command…";
    this.changed();
    try {
      const result = await this.api.confirm(prediction.id, payload);
      if (generation === this.generation) {
        this.phase = "idle";
        this.message = commandDescription(result);
        this.changed();
      }
      return result;
    } catch (error) {
      if (generation === this.generation) {
        this.phase = "idle";
        this.message = error.status === 409 || error.status === 410
          ? "This prediction was already consumed or expired. Capture another frame; this confirmation will not be retried."
          : "Confirmation failed or delivery is unknown. This confirmation will not be retried. Check the server/ESP32 and capture a new frame when ready.";
        this.changed();
      }
      throw error;
    }
  }

  async discard({ message } = {}) {
    const prediction = this.pending;
    const wasConfirming = this.phase === "confirming";
    const generation = ++this.generation;
    this.pending = null;
    this.phase = "idle";
    this.message = wasConfirming
      ? `${message || "Camera stopped."} A confirmation already submitted may still be delivered.`
      : message || "Prediction discarded. No command was requested.";
    this.changed();
    if (!prediction) return;
    try { await this.api.cancel(prediction.id); }
    catch {
      if (generation === this.generation) {
        this.message = `${message ? `${message} ` : ""}Prediction discarded locally. Server cancellation was not confirmed; it will expire without a command unless separately confirmed.`;
        this.changed();
      }
    }
  }

  expire() {
    if (this.phase !== "pending" || this.pending.expires_at === null || this.pending.expires_at > this.now()) return false;
    const id = this.pending.id;
    this.pending = null;
    this.phase = "idle";
    this.message = "This prediction expired. Capture a fresh frame before confirming a command.";
    this.api.cancel(id).catch(() => {});
    this.changed();
    return true;
  }
}

export function mountApp(document, {
  api = createApi(),
  makeCamera = (video, options) => new PhoneCamera(video, options),
  makeImage = () => new Image(),
  objectURL = globalThis.URL,
} = {}) {
  const element = id => document.getElementById(id);
  const camera = makeCamera(element("video"), { onEnded: handleCameraEnded });
  let frameUrl;
  let image;
  let capturing = false;
  let starting = false;
  let startGeneration = 0;
  let captureGeneration = 0;
  let modelReady = false;
  const workflow = new DepthWorkflow(api, { onChange: render });

  function render() {
    element("start").disabled = camera.running || starting;
    element("stop").disabled = !camera.running && !starting;
    element("capture").disabled = !camera.running || !workflow.canCapture || capturing;
    element("confirm").disabled = !workflow.canConfirm;
    element("discard").disabled = workflow.phase !== "pending";
    element("correction").disabled = !workflow.canConfirm;
    element("status").textContent = workflow.message;
    element("review").hidden = !workflow.pending;
    if (workflow.pending) {
      element("estimate").textContent = `${workflow.pending.selected.distance_cm.toFixed(2)} cm`;
      const seconds = workflow.pending.expires_at === null ? null : Math.max(0, Math.ceil(workflow.pending.expires_at - Date.now() / 1000));
      element("expiry").textContent = seconds === null ? "" : `Confirm within ${seconds} seconds.`;
    }
  }

  function drawRegions(result) {
    if (!image) return;
    const canvas = element("frame");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d");
    context.drawImage(image, 0, 0);
    context.font = `${Math.max(12, Math.round(canvas.width / 40))}px system-ui`;
    context.lineWidth = Math.max(2, canvas.width / 240);
    for (const object of result?.objects || []) {
      const selected = result.selected && object.box.every((value, i) => value === result.selected.box[i]) && object.distance_cm === result.selected.distance_cm;
      const [x1, y1, x2, y2] = object.box;
      context.strokeStyle = selected ? "#32e3a0" : "#f7c947";
      context.fillStyle = context.strokeStyle;
      context.strokeRect(x1, y1, x2 - x1, y2 - y1);
      context.fillText(`${selected ? "Closest: " : ""}${object.distance_cm.toFixed(2)} cm`, x1 + 3, Math.max(18, y1 - 5));
    }
    canvas.hidden = false;
  }

  element("start").addEventListener("click", async () => {
    const generation = ++startGeneration;
    starting = true;
    workflow.message = "Waiting for camera permission…";
    render();
    try {
      await camera.start();
      if (generation !== startGeneration) return;
      workflow.message = modelReady
        ? "Camera is running. Capture a frame to estimate depth."
        : "Camera is running. A trained model is required before depth estimation can work.";
    } catch (error) {
      if (generation === startGeneration) workflow.message = error.message;
    } finally {
      if (generation === startGeneration) {
        starting = false;
        render();
      }
    }
  });

  element("capture").addEventListener("click", async () => {
    if (capturing || !workflow.canCapture) return;
    const generation = ++captureGeneration;
    capturing = true;
    element("correction").value = "";
    render();
    try {
      const blob = await camera.capture();
      if (generation !== captureGeneration || !camera.running) return;
      if (frameUrl) objectURL.revokeObjectURL(frameUrl);
      frameUrl = objectURL.createObjectURL(blob);
      const nextImage = makeImage();
      nextImage.src = frameUrl;
      await nextImage.decode();
      if (generation !== captureGeneration || !camera.running) return;
      image = nextImage;
      drawRegions(null);
      const result = await workflow.analyze(blob);
      if (result && generation === captureGeneration) drawRegions(result);
    } catch (error) {
      if (generation === captureGeneration && workflow.phase === "idle" && !workflow.message.startsWith("Estimation failed") && !workflow.message.startsWith("No trained")) {
        workflow.message = error.message;
      }
    } finally {
      if (generation === captureGeneration) capturing = false;
      render();
    }
  });

  element("confirm").addEventListener("click", async () => {
    try { await workflow.confirm(element("correction").value); }
    catch (error) {
      if (workflow.phase === "pending") workflow.message = error.message;
      render();
    }
  });
  element("discard").addEventListener("click", () => { workflow.discard(); });

  function invalidateCamera(message) {
    startGeneration += 1;
    captureGeneration += 1;
    capturing = false;
    camera.stop();
    starting = false;
    workflow.discard({ message });
    render();
  }
  function stop() {
    invalidateCamera("Camera stopped. Start the camera and capture a fresh frame when ready.");
  }
  function handleCameraEnded() {
    invalidateCamera("Camera stream ended. Start the camera and capture a fresh frame.");
  }
  element("stop").addEventListener("click", stop);
  const timer = setInterval(() => { workflow.expire(); render(); }, 1000);
  globalThis.addEventListener?.("pagehide", () => {
    stop();
    if (frameUrl) objectURL.revokeObjectURL(frameUrl);
  });
  globalThis.addEventListener?.("pageshow", render);

  Promise.all([api.health(), api.config()]).then(([health, config]) => {
    modelReady = health.model_ready === true;
    element("model-status").textContent = modelReady
      ? "Trained depth model loaded."
      : "No trained depth model loaded. Supply RGB/depth pairs and train a checkpoint first.";
    element("delivery-status").textContent = config.delivery_enabled
      ? "Confirmed commands are sent to the configured ESP32 over Wi-Fi."
      : "Commands are recorded symbolically. Set ESP32_URL on the server to enable Wi-Fi delivery.";
    element("threshold").textContent = `${config.threshold_cm} cm`;
  }).catch(error => {
    element("model-status").textContent = `Server is unavailable: ${error.message}`;
  });
  render();
  return { workflow, camera, stop, dispose() { stop(); clearInterval(timer); } };
}

if (typeof document !== "undefined" && document.getElementById("depth-app")) mountApp(document);
