const CARD_TAG = "family-dashboard-card";
const DEFAULT_HEIGHT = 560;
const MIN_HEIGHT = 320;
const MAX_HEIGHT = 2000;

class FamilyDashboardCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          min-width: 0;
        }
        iframe {
          display: block;
          box-sizing: border-box;
          width: 100%;
          height: var(--family-dashboard-height, 560px);
          min-height: 320px;
          border: 0;
          border-radius: var(--ha-card-border-radius, 12px);
          background: var(--ha-card-background, var(--card-background-color, #ffffff));
          touch-action: auto;
        }
      </style>
      <iframe
        title="Family Dashboard"
        loading="eager"
        referrerpolicy="strict-origin-when-cross-origin"
        allow="fullscreen; clipboard-read; clipboard-write"
      ></iframe>
    `;
    this._url = "";
    this._title = "Family Dashboard";
    this._height = DEFAULT_HEIGHT;
  }

  static getStubConfig() {
    return {
      type: CARD_TAG,
      url: "https://dashboard.example.com",
      height: DEFAULT_HEIGHT,
    };
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Family Dashboard card configuration must be an object.");
    }

    this._url = this._validateUrl(config.url);
    this._title =
      typeof config.title === "string" && config.title.trim()
        ? config.title.trim()
        : "Family Dashboard";
    this._height = this._validateHeight(config.height);
    this.style.setProperty(
      "--family-dashboard-height",
      `${this._height}px`,
    );

    const iframe = this.shadowRoot.querySelector("iframe");
    iframe.title = this._title;
    if (iframe.src !== this._url) {
      iframe.src = this._url;
    }
  }

  getCardSize() {
    return Math.ceil(this._height / 50);
  }

  _validateUrl(value) {
    if (typeof value !== "string" || !value.trim()) {
      throw new Error("A Family Dashboard URL is required.");
    }

    let parsed;
    try {
      parsed = new URL(value.trim(), document.baseURI);
    } catch (_error) {
      throw new Error("The Family Dashboard URL is invalid.");
    }

    if (!["http:", "https:"].includes(parsed.protocol)) {
      throw new Error("The Family Dashboard URL must use HTTP or HTTPS.");
    }
    if (parsed.username || parsed.password) {
      throw new Error("Do not include credentials in the Family Dashboard URL.");
    }
    const sensitiveKeys = new Set([
      "access_token",
      "api_key",
      "authorization",
      "bearer",
      "token",
    ]);
    for (const key of parsed.searchParams.keys()) {
      if (sensitiveKeys.has(key.toLowerCase())) {
        throw new Error("Do not include credentials in the Family Dashboard URL.");
      }
    }

    return parsed.href;
  }

  _validateHeight(value) {
    if (value === undefined) {
      return DEFAULT_HEIGHT;
    }

    const height = Number(value);
    if (
      !Number.isFinite(height) ||
      height < MIN_HEIGHT ||
      height > MAX_HEIGHT
    ) {
      throw new Error(
        `Card height must be between ${MIN_HEIGHT} and ${MAX_HEIGHT} pixels.`,
      );
    }
    return Math.round(height);
  }
}

if (!customElements.get(CARD_TAG)) {
  customElements.define(CARD_TAG, FamilyDashboardCard);
}
