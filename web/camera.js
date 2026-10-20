/** Capture phone-camera frames. Camera permission requires HTTPS or localhost. */
export class PhoneCamera {
  constructor(video, {
    mediaDevices = globalThis.navigator?.mediaDevices,
    secureContext = globalThis.isSecureContext === true,
    createCanvas = () => document.createElement("canvas"),
  } = {}) {
    if (!video || typeof video.play !== "function") {
      throw new TypeError("A video element is required.");
    }
    this.video = video;
    this.mediaDevices = mediaDevices;
    this.secureContext = secureContext;
    this.createCanvas = createCanvas;
    this._stream = null;
    this._running = false;
    this._requestId = 0;
  }

  get running() {
    return this._running;
  }

  async start({ facingMode = "environment" } = {}) {
    this.stop();
    if (!this.secureContext) {
      throw new Error("Camera access requires HTTPS or localhost. Open this page in a secure context.");
    }
    if (typeof this.mediaDevices?.getUserMedia !== "function") {
      throw new Error("Camera access is unavailable in this browser. Use a browser with camera support.");
    }
    const requestId = this._requestId;
    let stream;
    try {
      stream = await this.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: facingMode } },
      });
      if (requestId !== this._requestId) {
        const cancelled = new Error("Camera start was cancelled.");
        cancelled.name = "AbortError";
        throw cancelled;
      }
      this._stream = stream;
      this.video.playsInline = true;
      this.video.muted = true;
      this.video.srcObject = stream;
      await this.video.play();
      if (requestId !== this._requestId) {
        const cancelled = new Error("Camera start was cancelled.");
        cancelled.name = "AbortError";
        throw cancelled;
      }
      this._running = true;
    } catch (error) {
      if (stream) this._release(stream);
      if (error.name === "NotAllowedError" || error.name === "SecurityError") {
        throw new Error("Camera permission was denied. Allow camera access in your browser settings and try again.", { cause: error });
      }
      if (error.name === "NotFoundError") {
        throw new Error("No camera was found on this device.", { cause: error });
      }
      throw error;
    }
  }

  async capture({ maxDimension = 640, quality = 0.85 } = {}) {
    if (!this.running) throw new Error("Start the camera before capturing a frame.");
    if (!Number.isInteger(maxDimension) || maxDimension < 1) {
      throw new RangeError("maxDimension must be a positive integer.");
    }
    if (!Number.isFinite(quality) || quality < 0 || quality > 1) {
      throw new RangeError("JPEG quality must be between 0 and 1.");
    }
    const { videoWidth: width, videoHeight: height } = this.video;
    if (!(width > 0 && height > 0)) {
      throw new Error("The camera has not produced a frame yet. Try again shortly.");
    }
    const scale = Math.min(1, maxDimension / Math.max(width, height));
    const canvas = this.createCanvas();
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("This browser cannot create a camera image.");
    context.drawImage(this.video, 0, 0, canvas.width, canvas.height);
    return new Promise((resolve, reject) => {
      canvas.toBlob(blob => {
        if (blob) resolve(blob);
        else reject(new Error("The browser could not encode the camera frame."));
      }, "image/jpeg", quality);
    });
  }

  _release(stream) {
    for (const track of stream.getTracks()) track.stop();
    if (this._stream === stream) {
      this._stream = null;
      this._running = false;
    }
    if (this.video.srcObject === stream) {
      this.video.pause();
      this.video.srcObject = null;
    }
  }

  stop() {
    this._requestId += 1;
    if (this._stream) this._release(this._stream);
    this._running = false;
  }
}
