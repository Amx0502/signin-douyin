#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import path from "node:path";
import vm from "node:vm";
import { Blob } from "node:buffer";
import { pathToFileURL } from "node:url";

import { casefold } from "./unicode_casefold.mjs";

export const SDK_BUNDLES = [
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/lib-polyfill.f81f86eb.js",
    sha256: "9076c568e86629b229e3f81f649d1a83e67d9645c3ba574406c3e6b2a804bc80",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/lib-router.5ab9ff10.js",
    sha256: "bbf164fe655433eaccfd346d87853b151f64a6391882c56dbc50a449e42095c8",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/2105.f8d74876.js",
    sha256: "a802f1c74efda6c83b3d26fd84b66980d88ea9bf12e87de1fbef4c6c4741bc4e",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/douyin_creator_data_old.2f971672.js",
    sha256: "77579ca1db8bd7ee874b27d6eb0691cdb36ef7eb11fb77c53cc81462e719d846",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/argus-builder-strategy.5a053c46.js",
    sha256: "1e8859043f62711f662f89710ee5e1c2c870af27d55dc174a2d7b09b70e1a121",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/7676.a4cd4900.js",
    sha256: "38cd5b2be2436fc9ce38b51b53045e4a8a2f05d9a581626b1969ed93722adab0",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/4916.56c33d22.js",
    sha256: "91e689e17d16f61386604daef544cfcb187fd02abc6885db1e5baf0f08381a7f",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/8198.b5c0b108.js",
    sha256: "56f7d2477591f3dd4951288354ad71d750a06ab5d7636adfc5640d323e29530d",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/4168.b2e72401.js",
    sha256: "d33a99a60aca01c0aa269adf112d4e49fc2436d734dd49fd209fa1faf1ec5a40",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/7771.d27d1891.js",
    sha256: "a96f33881f8fcd329aaa42546b324285efdb1f258419fa259be6bacde25c7c25",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/6682.2a991dfb.js",
    sha256: "4a63e6279be76182cb937f56587ebc77db91ba328579c238f84d4c8e4330a867",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/361.4fc40815.js",
    sha256: "02160b7dabc6a8437adb4f417b3975ef1e03058eae87bd72b36a1d8ba20bba78",
  },
  {
    url: "https://lf-fe-creator.douyinstatic.com/obj/douyn-creator-scm-cdn/douyin-creator-mono-pc-data/static/js/async/pages-chat.c817de31.js",
    sha256: "59ef0c6329bc5606cbf0a003d0836eaff56224caf0627820d5a8fcf11fa5c482",
  },
];

const CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat";

// 站点画像：与后端 app/douyin_site.py 对齐。
// 两侧是同一套 IM SDK（appKey 相同），差异只在 biz / appId / 请求头与 token 链路。
const SITE_PROFILES = {
  creator: {
    key: "creator",
    biz: "douyin_creator",
    appId: 2906,
    devicePlatform: "douyin_creator",
    origin: "https://creator.douyin.com",
    referer: CREATOR_CHAT_URL,
    // 请求包里附加的平台标识（creator 特有：aid_new/app_name）
    packetHeaders: { aid_new: 2906, app_name: "douyin_creator" },
    // creator 走 user_token + im/token 两段鉴权
    needIdentityApi: true,
    needImToken: true,
  },
  web: {
    key: "web",
    biz: "douyin_web",
    appId: 6383,
    devicePlatform: "douyin_pc",
    origin: "https://www.douyin.com",
    referer: "https://www.douyin.com/",
    // 主站请求包头的实际取值（HAR 实证：session_aid=6383 / app_name=douyin_pc）
    packetHeaders: {
      session_aid: "6383",
      session_did: "0",
      app_name: "douyin_pc",
      priority_region: "cn",
    },
    // 主站靠 cookie 会话鉴权（SESSION_AUTH），无需 user_token / im token
    needIdentityApi: false,
    needImToken: false,
  },
};

function resolveSite(payload) {
  const key = String(payload?.site || process.env.DOUYIN_SITE || "creator")
    .trim()
    .toLowerCase();
  return SITE_PROFILES[key] || SITE_PROFILES.creator;
}

// Node vm 沙箱里的浏览器 location 模拟（SDK 会据此推导来源域）
function buildLocationShim() {
  const site = activeSite;
  let pathname = "/";
  let hostname = "";
  try {
    const url = new URL(site.referer);
    pathname = url.pathname || "/";
    hostname = url.hostname;
  } catch {
    hostname = String(site.origin || "").replace(/^https?:\/\//, "");
  }
  return {
    href: site.referer,
    protocol: "https:",
    search: "",
    pathname,
    hostname,
  };
}

const USER_AGENT =
  (process.env.SPARKFLOW_PROTOCOL_USER_AGENT ||
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36").trim();

function noop() {}

function toCookieString(cookies) {
  return (cookies || [])
    .filter((item) => item?.name && item?.value !== undefined)
    .map((item) => `${item.name}=${item.value}`)
    .join("; ");
}

function normalizeNickname(value) {
  return casefold(value)
    .replace(/[\u200b-\u200d\ufeff]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function stableNow() {
  return new Date().toISOString();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function toNonNegativeInteger(value, fallback = 0) {
  const parsed = Number.parseInt(value, 10);
  if (Number.isNaN(parsed) || parsed < 0) {
    return fallback;
  }
  return parsed;
}

function normalizeSendStrategy(raw = {}) {
  const intervalMin = toNonNegativeInteger(raw.messageIntervalSecondsMin, 0);
  const intervalMax = Math.max(intervalMin, toNonNegativeInteger(raw.messageIntervalSecondsMax, intervalMin));
  return {
    messageIntervalSecondsMin: intervalMin,
    messageIntervalSecondsMax: intervalMax,
  };
}

function randomBetweenInclusive(min, max) {
  if (max <= min) {
    return min;
  }
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

const SEND_MESSAGE_STATUS_NAMES = {
  0: "Succeeded",
  1: "UserNotInConversation",
  2: "CheckConversationNotPass",
  3: "CheckMessageNotPass",
  4: "CheckMessageNotPassButSelfVisible",
  5: "UserHasBeenBlock",
};

function sendMessageStatusName(statusCode) {
  if (statusCode === null || statusCode === undefined) {
    return "";
  }
  return SEND_MESSAGE_STATUS_NAMES[Number(statusCode)] || "Unknown";
}

export function isSuccessfulSendResult(sendResult) {
  return (
    Boolean(sendResult?.success) &&
    Number(sendResult?.statusCode) === 0
  );
}

function publicSendResultSummary(sendResult) {
  if (!sendResult || typeof sendResult !== "object") {
    return {};
  }
  const summary = {};
  for (const key of ["success", "statusCode", "statusMsg", "checkCode", "checkMsg", "errorCode", "errorMsg"]) {
    if (sendResult[key] !== undefined) {
      summary[key] = sendResult[key];
    }
  }
  summary.rawKeys = Object.keys(sendResult).sort();
  return summary;
}

async function readStdinJson() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) {
    throw new Error("Missing JSON payload on stdin");
  }
  return JSON.parse(raw);
}

export function sha256Hex(body) {
  const buffer = Buffer.isBuffer(body) ? body : Buffer.from(body);
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

export function verifyBundleBytes(body, expectedSha256) {
  const actual = sha256Hex(body);
  const expected = String(expectedSha256 || "").toLowerCase();
  if (!expected || actual !== expected) {
    throw new Error(
      `Bundle integrity check failed: expected=${expected || "missing"} actual=${actual}`,
    );
  }
  return actual;
}

async function ensureBundles(cacheDir) {
  await fs.promises.mkdir(cacheDir, { recursive: true });
  for (const bundle of SDK_BUNDLES) {
    const { url, sha256 } = bundle;
    const filename = url.split("/").at(-1);
    const filePath = path.join(cacheDir, filename);
    if (fs.existsSync(filePath)) {
      const cached = await fs.promises.readFile(filePath);
      try {
        verifyBundleBytes(cached, sha256);
        continue;
      } catch {
        // A stale or modified cache entry is replaced only after the download verifies.
      }
    }
    const response = await fetch(url, { headers: { "User-Agent": USER_AGENT } });
    if (!response.ok) {
      throw new Error(`Failed to download SDK bundle ${url}: ${response.status}`);
    }
    const body = Buffer.from(await response.arrayBuffer());
    verifyBundleBytes(body, sha256);
    await fs.promises.writeFile(filePath, body);
  }
}

function createWebpackRequire(bundleDir, cookieString) {
  const modules = {};
  const cache = {};

  function requireModule(id) {
    if (cache[id]) {
      return cache[id].exports;
    }
    if (!modules[id]) {
      throw new Error(`Missing webpack module ${id}`);
    }
    const module = { exports: {} };
    cache[id] = module;
    modules[id].call(module.exports, module, module.exports, requireModule);
    return module.exports;
  }

  requireModule.d = (exports, definition) => {
    for (const key of Object.keys(definition)) {
      if (!Object.prototype.hasOwnProperty.call(exports, key)) {
        Object.defineProperty(exports, key, {
          enumerable: true,
          get: definition[key],
        });
      }
    }
  };
  requireModule.o = (obj, prop) => Object.prototype.hasOwnProperty.call(obj, prop);
  requireModule.r = (exports) => {
    if (typeof Symbol !== "undefined" && Symbol.toStringTag) {
      Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
    }
    Object.defineProperty(exports, "__esModule", { value: true });
  };
  requireModule.n = (mod) => {
    const getter = mod && mod.__esModule ? () => mod.default : () => mod;
    requireModule.d(getter, { a: getter });
    return getter;
  };
  requireModule.g = globalThis;
  requireModule.hmd = (module) => module;
  requireModule.nmd = (module) => module;

  const chunkArray = [];
  chunkArray.push = (chunk) => Object.assign(modules, chunk[1]);

  const fakeElement = () => ({
    style: {},
    setAttribute: noop,
    appendChild: noop,
    removeChild: noop,
    addEventListener: noop,
    removeEventListener: noop,
    getContext: () => ({}),
  });
  const documentRef = {
    cookie: cookieString,
    referrer: CREATOR_CHAT_URL,
    createElement: fakeElement,
    getElementsByTagName: () => [],
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: noop,
    removeEventListener: noop,
    body: { appendChild: noop, removeChild: noop },
    head: { appendChild: noop, removeChild: noop },
    documentElement: { style: {} },
  };
  function XMLHttpRequestStub() {
    this.open = noop;
    this.setRequestHeader = noop;
    this.send = noop;
  }
  function WebSocketStub() {
    this.readyState = 1;
    this.send = noop;
    this.close = noop;
  }

  const context = {
    self: { webpackChunkdouyin_creator_data: chunkArray },
    window: {},
    globalThis: null,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Buffer,
    TextDecoder,
    TextEncoder,
    Blob,
    document: documentRef,
    navigator: {
      userAgent: USER_AGENT,
      language: "en-US",
      cookieEnabled: true,
      onLine: true,
      platform: "Linux x86_64",
      sendBeacon: undefined,
      appName: "Netscape",
    },
    location: buildLocationShim(),
    localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    performance: { now: () => Date.now() },
    fetch: (...args) => activeFetch(...args),
    XMLHttpRequest: XMLHttpRequestStub,
    WebSocket: WebSocketStub,
    URL,
    URLSearchParams,
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    crypto,
  };
  context.window = context;
  context.globalThis = context;

  // 2026-10-14 优化：IM SDK bundle 很大，每次 vm 编译就要 ~22s（实测 sdk_init
  // 占总耗时的全部）。改用 **V8 code cache**（vm.Script 的 cachedData）：首次
  // 编译后把字节码缓存到 <bundle>.v8cache，之后命中即跳过编译。
  // 缓存与源码 hash / V8 版本绑定，不匹配时 V8 静默重新编译，不会出错。
  const vmContext = vm.createContext(context);
  for (const entry of fs.readdirSync(bundleDir).filter((name) => name.endsWith(".js")).sort()) {
    const filePath = path.join(bundleDir, entry);
    const code = fs.readFileSync(filePath, "utf8");
    const cachePath = `${filePath}.v8cache`;
    let cachedData;
    try {
      cachedData = fs.readFileSync(cachePath);
    } catch {
      cachedData = undefined;
    }
    try {
      const script = new vm.Script(code, { filename: entry, cachedData });
      script.runInContext(vmContext);
      try {
        const produced = script.createCachedData();
        if (produced && produced.length) {
          fs.writeFileSync(cachePath, produced);
        }
      } catch {
        // 缓存写盘失败不影响主流程（下次重新编译）
      }
    } catch {
      // Some bundles execute browser-only entrypoints after registering modules.
    }
  }

  return requireModule;
}

class ProtocolError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = "ProtocolError";
    this.details = details;
  }
}

function extractCookieMap(cookies) {
  const items = {};
  for (const item of cookies || []) {
    if (item?.name) {
      items[item.name] = item.value ?? "";
    }
  }
  return items;
}

/**
 * 代理感知的受限 fetch：HTTP(S) 代理经 CONNECT 隧道承载 https 请求，
 * 覆盖本脚本与 IM SDK 的全部出站 HTTP（WebSocket 为 stub，不涉及）。
 * 未配置代理时直接用原生 fetch；socks5 不支持（提示改用 http 代理）。
 */
let activeFetch = globalThis.fetch;

function buildProxyFetch(proxy) {
  if (!proxy || !proxy.host || !proxy.port) {
    return globalThis.fetch;
  }
  if (String(proxy.protocol || "").toLowerCase() === "socks5") {
    throw new Error("protocol_sender 暂不支持 socks5 代理，请改用 http/https 代理");
  }
  const authHeader = proxy.username
    ? "Basic " + Buffer.from(`${proxy.username}:${proxy.password || ""}`).toString("base64")
    : "";

  function connectTunnel(targetHost, targetPort, timeoutMs) {
    return new Promise((resolve, reject) => {
      const req = http.request({
        host: proxy.host,
        port: Number(proxy.port),
        method: "CONNECT",
        path: `${targetHost}:${targetPort}`,
        headers: {
          Host: `${targetHost}:${targetPort}`,
          ...(authHeader ? { "Proxy-Authorization": authHeader } : {}),
        },
        timeout: timeoutMs,
      });
      req.on("connect", (res, socket) => {
        if (res.statusCode !== 200) {
          socket.destroy();
          reject(new Error(`代理 CONNECT 失败：${res.statusCode}`));
          return;
        }
        resolve(socket);
      });
      req.on("timeout", () => req.destroy(new Error("代理连接超时")));
      req.on("error", reject);
      req.end();
    });
  }

  return async function proxiedFetch(url, options = {}) {
    const timeoutMs = options.timeoutMs || 15000;
    const target = new URL(String(url));
    if (target.protocol !== "https:") {
      throw new Error("代理通道仅支持 https 目标");
    }
    const socket = await connectTunnel(target.hostname, Number(target.port) || 443, timeoutMs);
    return await new Promise((resolve, reject) => {
      let settled = false;
      const req = https.request({
        socket,
        servername: target.hostname,
        host: target.hostname,
        port: Number(target.port) || 443,
        method: String(options.method || "GET").toUpperCase(),
        path: target.pathname + target.search,
        headers: options.headers || {},
        timeout: timeoutMs,
      }, (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          if (settled) return;
          settled = true;
          const buffer = Buffer.concat(chunks);
          const text = buffer.toString("utf8");
          resolve({
            ok: res.statusCode >= 200 && res.statusCode < 300,
            status: res.statusCode,
            statusText: res.statusMessage || "",
            headers: { get: (name) => res.headers[String(name).toLowerCase()] ?? null },
            text: async () => text,
            json: async () => JSON.parse(text),
            arrayBuffer: async () =>
              buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength),
          });
        });
      });
      req.on("error", (error) => {
        if (!settled) {
          settled = true;
          reject(error);
        }
      });
      req.on("timeout", () => req.destroy(new Error("请求超时")));
      const signal = options.signal;
      if (signal) {
        const abort = () => {
          if (!settled) {
            settled = true;
            req.destroy(new Error("aborted"));
            reject(new Error("aborted"));
          }
        };
        if (signal.aborted) abort();
        else signal.addEventListener("abort", abort, { once: true });
      }
      const body = options.body;
      if (body != null) {
        if (typeof body === "string") req.write(body);
        else if (body instanceof URLSearchParams) req.write(body.toString());
        else if (body instanceof Uint8Array) req.write(Buffer.from(body));
        else req.write(String(body));
      }
      req.end();
    });
  };
}

// 当前活动的站点画像（main 里按 payload.site 设置；默认 creator 保持旧行为）
let activeSite = SITE_PROFILES.creator;

function buildCreatorHeaders(cookieString, cookieMap, referer) {
  const site = activeSite;
  return {
    "User-Agent": USER_AGENT,
    Referer: referer || site.referer,
    Origin: site.origin,
    Accept: "application/json, text/javascript",
    "Content-Type": "application/x-www-form-urlencoded",
    Cookie: cookieString,
    "x-tt-passport-csrf-token":
      cookieMap.passport_csrf_token || cookieMap.passport_csrf_token_default || "",
  };
}

function buildImHeaders(cookieString) {
  const site = activeSite;
  return {
    "User-Agent": USER_AGENT,
    Referer: site.referer,
    Origin: site.origin,
    Cookie: cookieString,
  };
}

async function fetchJson(url, options = {}) {
  const timeoutMs = options.timeoutMs || 15000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await activeFetch(url, {
      ...options,
      signal: controller.signal,
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    return { response, text, data };
  } finally {
    clearTimeout(timer);
  }
}

async function fetchSessionIdentity(cookieString, cookieMap) {
  const headers = buildCreatorHeaders(cookieString, cookieMap);
  const params = new URLSearchParams({
    aid: "2906",
    app_name: "aweme_creator_platform",
    device_platform: "web",
    referer: "",
    user_agent: USER_AGENT,
    cookie_enabled: "true",
    screen_width: "1280",
    screen_height: "720",
    browser_language: "en-US@posix",
    browser_platform: "Linux x86_64",
    browser_name: "Mozilla",
    browser_version: USER_AGENT,
    browser_online: "true",
    timezone_name: "Asia/Shanghai",
  });
  const { response, data, text } = await fetchJson(
    `https://creator.douyin.com/aweme/v1/creator/im/user_token/?${params.toString()}`,
    { headers },
  );
  if (!response.ok || data?.status_code !== 0 || !data?.user_id) {
    throw new ProtocolError("Failed to resolve creator IM session identity", {
      status: response.status,
      body: text,
    });
  }
  return {
    userId: String(data.user_id),
    sessionToken: String(data.token || ""),
  };
}

async function fetchImToken(cookieString, cookieMap) {
  // 真正的 IM 会话 token 来自 /web/api/v1/im/token/（channel=24 是 creator 私信通道）。
  // 注意：/aweme/v1/creator/im/user_token/ 只返回 user_id、**不返回 token**；若 SDK 以
  // 空 token 初始化（SESSION_AUTH），消息发送会被服务端安全校验拒绝
  // （statusCode=3 CheckMessageNotPass，2026-10-09 线上实测 root cause）。
  const headers = {
    ...buildCreatorHeaders(cookieString, cookieMap),
    "Content-Type": "application/json",
  };
  const { response, data, text } = await fetchJson(
    "https://creator.douyin.com/web/api/v1/im/token/",
    {
      method: "POST",
      headers,
      body: JSON.stringify({ channel: 24, app_id: 10001, is_visitor: false }),
    },
  );
  if (!response.ok || Number(data?.code) !== 200 || !data?.data?.token) {
    throw new ProtocolError("Failed to resolve creator IM token", {
      status: response.status,
      body: text,
    });
  }
  return String(data.data.token);
}

async function fetchIdentitySecurityToken(cookieString, cookieMap) {
  const headers = buildCreatorHeaders(cookieString, cookieMap);
  const params = new URLSearchParams({
    scene: "im_send_msg",
    auto_retry_req: "0",
    skip_verify: "0",
    identity_token_force_get_tag: "0",
    passport_jssdk_version: "5.1.4",
    passport_jssdk_type: "lite",
    is_from_ttaccountsdk: "1",
    aid: "2906",
    language: "zh",
    account_app_language: "en-US",
    id_token_version: "2.1.5",
  });
  const { response, data, text } = await fetchJson(
    `https://creator.douyin.com/passport/safe/get_identity_security_token/?${params.toString()}`,
    { headers },
  );
  if (!response.ok || data?.message !== "success" || !data?.data?.identity_security_token) {
    throw new ProtocolError("Failed to resolve identity security token", {
      status: response.status,
      body: text,
    });
  }
  return {
    identitySecurityHeader: JSON.stringify({ token: data.data.identity_security_token }),
    realDeviceId: String(data.data.device_id || ""),
  };
}

async function fetchProfileNickname(cookieString, secUid) {
  const url =
    "https://www.douyin.com/aweme/v1/web/user/profile/other/?" +
    new URLSearchParams({ sec_user_id: secUid }).toString();
  const { response, data, text } = await fetchJson(url, {
    headers: {
      "User-Agent": USER_AGENT,
      Referer: `https://www.douyin.com/user/${secUid}`,
      Cookie: cookieString,
      Accept: "application/json, text/javascript",
    },
  });
  if (!response.ok || data?.status_code !== 0) {
    return { nickname: "", uniqueId: "", avatar: "" };
  }
  const user = data?.user || {};
  const avatarList = user?.avatar_thumb?.url_list || [];
  // 返回原始昵称（不折叠大小写），仅在参与匹配时再 normalizeNickname，
  // 这样好友列表可以按用户看到的大小写展示。
  return {
    nickname: String(user?.nickname || "").trim(),
    uniqueId: String(user?.unique_id || "").trim(),
    avatar: String(avatarList[avatarList.length - 1] || "").trim(),
  };
}

function stringifyMaybeLong(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "bigint") {
    return String(value);
  }
  if (typeof value.toString === "function" && value.toString !== Object.prototype.toString) {
    return value.toString();
  }
  return String(value);
}

function selectPeerParticipant(conversation, selfUserId) {
  const participants = conversation?.firstPageParticipant?.participants || [];
  for (const participant of participants) {
    const currentUserId = stringifyMaybeLong(participant?.user_id);
    if (currentUserId && currentUserId !== selfUserId) {
      return participant;
    }
  }
  return null;
}

async function createProtocolClient({ bundleDir, cookieString, cookieMap, userId, imToken = "" }) {
  const site = activeSite;
  const requireModule = createWebpackRequire(bundleDir, cookieString);
  const sdk = requireModule(61724);
  const { BytedIM } = requireModule(26440);

  class AdditionalParamsPlugin extends sdk.BasePlugin {
    install() {}

    async sendPacket(packet) {
      packet.device_id = 0;
      packet.device_platform = site.devicePlatform;
      packet.headers = {
        ...(packet.headers || {}),
        ...site.packetHeaders,
      };
      return packet;
    }
  }

  class NodeHttpClient extends sdk.IMHttpClient {
    async send(url, method, body) {
      const fullUrl = /^https?:/i.test(url)
        ? url
        : `${String(this.option.apiUrl).replace(/\/$/, "")}/${String(url).replace(/^\//, "")}`;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch(fullUrl, {
          method,
          headers: this.headers,
          body: body ? Buffer.from(body) : undefined,
          signal: controller.signal,
        });
        return await response.arrayBuffer();
      } finally {
        clearTimeout(timer);
      }
    }

    sendByBeacon() {
      return false;
    }
  }

  const client = new BytedIM(
    {
      appId: site.appId,
      fpId: 9,
      appKey: "e1bd35ec9db7b8d846de66ed140b1ad9",
      service: 5,
      apiUrl: "https://imapi.douyin.com",
      frontierUrl: "wss://frontier-im.douyin.com/ws/v2",
      inboxType: 1,
      token: imToken,
      userId,
      deviceId: userId,
      authType: sdk.im_proto.AuthType.SESSION_AUTH,
      devicePlatform: site.devicePlatform,
      timeout: 20000,
      acceptIncorrectInboxType: true,
      biz: site.biz,
      withCredentials: false,
      httpHeaders: buildImHeaders(cookieString),
      headers: {},
      webSocketLevel: sdk.WebSocketLevel.PushOnly,
      debug: false,
      http: (ctx) => new NodeHttpClient(ctx),
    },
    [AdditionalParamsPlugin],
  );

  const initResult = await client.init();
  if (initResult !== sdk.InitResult.Succeeded) {
    throw new ProtocolError("Protocol IM init did not succeed", { initResult });
  }

  return { client };
}

async function buildConversationCache({
  client,
  selfUserId,
  cookieString,
  existingCache = [],
  targetNames = [],
}) {
  const cachedBySecUid = new Map(
    (existingCache || []).filter((entry) => entry?.secUid).map((entry) => [entry.secUid, entry]),
  );
  const wantedTargets = new Set((targetNames || []).map(normalizeNickname).filter(Boolean));
  const matchedTargets = new Set();
  const conversations = await client.getConversationListOnline();
  const cacheEntries = [];

  for (const conversation of conversations) {
    if (conversation?.type !== 1) {
      continue;
    }

    const peer = selectPeerParticipant(conversation, selfUserId);
    if (!peer) {
      continue;
    }

    const peerUserId = stringifyMaybeLong(peer.user_id);
    const secUid = peer.sec_uid || "";
    if (!peerUserId && !secUid) {
      continue;
    }

    let profile = secUid ? cachedBySecUid.get(secUid) : null;
    let nickname = String(profile?.nickname || "").trim();
    if (!nickname && secUid) {
      try {
        profile = await fetchProfileNickname(cookieString, secUid);
        nickname = String(profile?.nickname || "").trim();
      } catch {
        profile = null;
        nickname = "";
      }
    }

    cacheEntries.push({
      nickname,
      uniqueId: String(profile?.uniqueId || "").trim(),
      avatar: String(profile?.avatar || "").trim(),
      peerUserId,
      secUid,
      conversationId: conversation.id,
      conversationShortId: conversation.shortId,
      updatedAt: stableNow(),
    });

    const matchKey = normalizeNickname(nickname);
    if (matchKey && wantedTargets.has(matchKey)) {
      matchedTargets.add(matchKey);
      if (matchedTargets.size === wantedTargets.size) {
        break;
      }
    }
  }

  return mergeConversationCache(existingCache, cacheEntries);
}

/**
 * 拉取会话列表（= 可私信好友），返回带昵称的条目，供前端展示与挑选目标。
 * 昵称为原始大小写，参与发送匹配时由 buildTargetLookup 再折叠。
 */
export async function collectFriends({ client, selfUserId, cookieString, existingCache = [] }) {
  const cachedBySecUid = new Map(
    (existingCache || []).filter((entry) => entry?.secUid).map((entry) => [entry.secUid, entry]),
  );
  const conversations = await client.getConversationListOnline();
  const friends = [];
  const seen = new Set();

  for (const conversation of conversations) {
    if (conversation?.type !== 1) {
      continue;
    }
    const peer = selectPeerParticipant(conversation, selfUserId);
    if (!peer) {
      continue;
    }
    const peerUserId = stringifyMaybeLong(peer.user_id);
    const secUid = String(peer.sec_uid || "").trim();
    if (!peerUserId && !secUid) {
      continue;
    }
    const dedupeKey = secUid || peerUserId;
    if (seen.has(dedupeKey)) {
      continue;
    }
    seen.add(dedupeKey);

    let profile = secUid ? cachedBySecUid.get(secUid) : null;
    let nickname = String(profile?.nickname || "").trim();
    if (!nickname && secUid) {
      try {
        profile = await fetchProfileNickname(cookieString, secUid);
        nickname = String(profile?.nickname || "").trim();
      } catch {
        profile = null;
        nickname = "";
      }
    }

    friends.push({
      nickname,
      uniqueId: String(profile?.uniqueId || "").trim(),
      avatar: String(profile?.avatar || "").trim(),
      peerUserId,
      secUid,
      conversationId: stringifyMaybeLong(conversation.id),
      conversationShortId: stringifyMaybeLong(conversation.shortId),
      updatedAt: stableNow(),
    });
  }

  friends.sort((left, right) => {
    if (Boolean(left.nickname) !== Boolean(right.nickname)) {
      return left.nickname ? -1 : 1;
    }
    return left.nickname.localeCompare(right.nickname, "zh-CN");
  });
  return friends;
}

/**
 * 解析单个会话的「连续聊天/火花」状态。
 *
 * 数据源（实测确认，见 douyin-sparkflow-plan.md §10.7）：
 *   conversation.coreInfo.innerExt["a:consecutive_chat_data"] → JSON 字符串
 *
 * 返回 null = 该会话没有火花记录（不必续）。
 * 否则返回 { state, days, realDays, level, text, recoverDdl, canRecoverDays, expireTime, active }：
 *   state: 0=无进行中火花（仅历史记录） / 1=今日已续 / 2=今日未续 / 3=濒危
 *   days:  连续天数（优先 consecutive_count_info.consecutive_count）
 *   text:  平台渲染文案（如「点亮中 1/3」「505」「6 天后消失」），前端可直接展示
 *   active: state ∈ {1,2,3}（存在进行中的火花时间轴）
 */
export function parseSparkState(conversation) {
  const ext = conversation?.coreInfo?.innerExt || {};
  const raw = ext["a:consecutive_chat_data"];
  if (typeof raw !== "string" || !raw.trim()) {
    return null;
  }
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  const infos = Array.isArray(data.flame_infos) ? data.flame_infos : [];
  const today = infos[0] || {};
  const countInfo = data.consecutive_count_info || {};
  const state = Number(today.state || 0);
  const active = [1, 2, 3].includes(state);
  const daysRaw = Number(
    countInfo.consecutive_count ?? today.real_days ?? today.days ?? 0,
  );
  const days = Number.isFinite(daysRaw) ? daysRaw : 0;
  const daysText = String(today.text || today.simplify_text || "").trim();
  return {
    state,
    active,
    // state=0 时只有历史记录，连续天数不具"当前"含义 → 归零避免前端误显示
    days: active ? days : 0,
    realDays: Number.isFinite(Number(today.real_days)) ? Number(today.real_days) : null,
    level: String(today.level || ""),
    text: daysText,
    recoverDdl: today.recover_ddl ?? null,
    canRecoverDays: data.can_recover_days ?? data.rcov_d ?? null,
    expireTime: countInfo.expire_time ?? null,
  };
}

/** 「曾否有火花」的轻量信号：coreInfo.innerExt["a:pre_flame_parenting"] */
export function hasPreFlame(conversation) {
  const ext = conversation?.coreInfo?.innerExt || {};
  const raw = ext["a:pre_flame_parenting"];
  if (typeof raw !== "string" || !raw.trim()) {
    return false;
  }
  try {
    return Number(JSON.parse(raw)?.has_pre_flame || 0) > 0;
  } catch {
    return false;
  }
}

/**
 * 拉取全部会话的火花状态（只读，不发送任何消息）。
 * 返回带昵称的条目，spark 为 null 表示该会话无火花记录。
 */
export async function collectSparkStates({ client, selfUserId, cookieString, existingCache = [] }) {
  const cachedBySecUid = new Map(
    (existingCache || []).filter((entry) => entry?.secUid).map((entry) => [entry.secUid, entry]),
  );
  const conversations = await client.getConversationListOnline();
  const results = [];
  const seen = new Set();

  for (const conversation of conversations) {
    if (conversation?.type !== 1) {
      continue;
    }
    const peer = selectPeerParticipant(conversation, selfUserId);
    if (!peer) {
      continue;
    }
    const peerUserId = stringifyMaybeLong(peer.user_id);
    const secUid = String(peer.sec_uid || "").trim();
    const dedupeKey = secUid || peerUserId;
    if (!dedupeKey || seen.has(dedupeKey)) {
      continue;
    }
    seen.add(dedupeKey);

    const spark = parseSparkState(conversation);
    const preFlame = hasPreFlame(conversation);
    if (!spark && !preFlame) {
      // 无火花记录且从未有过火花 → 跳过，前端不必展示
      continue;
    }

    let profile = secUid ? cachedBySecUid.get(secUid) : null;
    let nickname = String(profile?.nickname || "").trim();
    if (!nickname && secUid) {
      try {
        profile = await fetchProfileNickname(cookieString, secUid);
        nickname = String(profile?.nickname || "").trim();
      } catch {
        profile = null;
        nickname = "";
      }
    }

    results.push({
      nickname,
      uniqueId: String(profile?.uniqueId || "").trim(),
      avatar: String(profile?.avatar || "").trim(),
      peerUserId,
      secUid,
      conversationId: stringifyMaybeLong(conversation.id),
      conversationShortId: stringifyMaybeLong(conversation.shortId),
      spark,
      updatedAt: stableNow(),
    });
  }

  results.sort((left, right) => {
    const leftDays = Number(left?.spark?.days || 0);
    const rightDays = Number(right?.spark?.days || 0);
    if (leftDays !== rightDays) {
      return rightDays - leftDays;
    }
    return String(left.nickname || "").localeCompare(String(right.nickname || ""), "zh-CN");
  });
  return results;
}

/**
 * 一次会话列表采样，同时产出「好友列表」与「火花状态」两份数据。
 *
 * 拉好友（collectFriends）与读火花（collectSparkStates）的数据源完全相同，
 * 都是 client.getConversationListOnline()；拆成两次调用就要各付一次 ~22s 的
 * SDK 初始化成本。合并后一次拿两份，昵称/头像也只在缺失时取一次。
 */
export async function collectFriendsAndSparks({
  client,
  selfUserId,
  cookieString,
  existingCache = [],
}) {
  const cachedBySecUid = new Map(
    (existingCache || []).filter((entry) => entry?.secUid).map((entry) => [entry.secUid, entry]),
  );
  const conversations = await client.getConversationListOnline();
  const friends = [];
  const sparks = [];
  const seen = new Set();

  for (const conversation of conversations) {
    if (conversation?.type !== 1) {
      continue;
    }
    const peer = selectPeerParticipant(conversation, selfUserId);
    if (!peer) {
      continue;
    }
    const peerUserId = stringifyMaybeLong(peer.user_id);
    const secUid = String(peer.sec_uid || "").trim();
    const dedupeKey = secUid || peerUserId;
    if (!dedupeKey || seen.has(dedupeKey)) {
      continue;
    }
    seen.add(dedupeKey);

    // 昵称/头像只取一次，好友与火花两份结果共用（与 collectFriends 同逻辑）
    let profile = secUid ? cachedBySecUid.get(secUid) : null;
    let nickname = String(profile?.nickname || "").trim();
    if (!nickname && secUid) {
      try {
        profile = await fetchProfileNickname(cookieString, secUid);
        nickname = String(profile?.nickname || "").trim();
      } catch {
        profile = null;
        nickname = "";
      }
    }

    const conversationId = stringifyMaybeLong(conversation.id);
    const conversationShortId = stringifyMaybeLong(conversation.shortId);
    const uniqueId = String(profile?.uniqueId || "").trim();
    const avatar = String(profile?.avatar || "").trim();

    friends.push({
      nickname,
      uniqueId,
      avatar,
      peerUserId,
      secUid,
      conversationId,
      conversationShortId,
      updatedAt: stableNow(),
    });

    const spark = parseSparkState(conversation);
    const preFlame = hasPreFlame(conversation);
    if (!spark && !preFlame) {
      // 无火花记录且从未有过火花 → 不进火花列表，前端不必展示
      continue;
    }
    sparks.push({
      nickname,
      uniqueId,
      avatar,
      peerUserId,
      secUid,
      conversationId,
      conversationShortId,
      spark,
      updatedAt: stableNow(),
    });
  }

  friends.sort((left, right) => {
    if (Boolean(left.nickname) !== Boolean(right.nickname)) {
      return left.nickname ? -1 : 1;
    }
    return left.nickname.localeCompare(right.nickname, "zh-CN");
  });
  sparks.sort((left, right) => {
    const leftDays = Number(left?.spark?.days || 0);
    const rightDays = Number(right?.spark?.days || 0);
    if (leftDays !== rightDays) {
      return rightDays - leftDays;
    }
    return String(left.nickname || "").localeCompare(String(right.nickname || ""), "zh-CN");
  });
  return { friends, sparks };
}

export function mergeConversationCache(existingCache, cacheEntries) {
  const deduped = new Map();
  const bySecUid = new Map();
  const byPeerUserId = new Map();

  function removeEntry(entry) {
    const secUid = String(entry?.secUid || "").trim();
    const peerUserId = String(entry?.peerUserId || "").trim();
    if (secUid) {
      bySecUid.delete(secUid);
    }
    if (peerUserId) {
      byPeerUserId.delete(peerUserId);
    }
  }

  function addEntry(entry) {
    const secUid = String(entry?.secUid || "").trim();
    const peerUserId = String(entry?.peerUserId || "").trim();
    if (!entry?.nickname || (!secUid && !peerUserId)) {
      return;
    }
    const matches = [];
    for (const candidate of [
      secUid && bySecUid.get(secUid),
      peerUserId && byPeerUserId.get(peerUserId),
    ]) {
      if (candidate && !matches.includes(candidate)) {
        matches.push(candidate);
      }
    }
    const merged = {};
    for (const existing of [...matches, entry]) {
      for (const field of [
        "nickname",
        "uniqueId",
        "avatar",
        "secUid",
        "peerUserId",
        "conversationId",
        "conversationShortId",
        "updatedAt",
      ]) {
        const value = String(existing[field] || "").trim();
        if (value) {
          merged[field] = existing[field];
        }
      }
    }
    for (const existing of matches) {
      removeEntry(existing);
      for (const [key, value] of deduped) {
        if (value === existing) {
          deduped.delete(key);
        }
      }
    }
    const mergedSecUid = String(merged.secUid || "").trim();
    const mergedPeerUserId = String(merged.peerUserId || "").trim();
    const key = mergedSecUid
      ? `sec:${mergedSecUid}`
      : `peer:${mergedPeerUserId}`;
    deduped.set(key, merged);
    if (mergedSecUid) {
      bySecUid.set(mergedSecUid, merged);
    }
    if (mergedPeerUserId) {
      byPeerUserId.set(mergedPeerUserId, merged);
    }
  }

  for (const entry of existingCache || []) {
    addEntry(entry);
  }
  for (const entry of cacheEntries) {
    addEntry(entry);
  }
  return Array.from(deduped.values()).sort((left, right) =>
    left.nickname.localeCompare(right.nickname, "zh-CN"),
  );
}

function addUniqueLookupEntry(lookup, key, entry) {
  const normalized = String(key || "").trim();
  if (!normalized) {
    return;
  }
  if (lookup.has(normalized) && lookup.get(normalized) !== entry) {
    lookup.set(normalized, null);
    return;
  }
  lookup.set(normalized, entry);
}


export function buildTargetLookup(cacheEntries) {
  const byNickname = new Map();
  const bySecUid = new Map();
  const byPeerUserId = new Map();
  for (const entry of cacheEntries) {
    const key = normalizeNickname(entry.nickname);
    addUniqueLookupEntry(byNickname, key, entry);
    addUniqueLookupEntry(bySecUid, entry.secUid, entry);
    addUniqueLookupEntry(byPeerUserId, entry.peerUserId, entry);
  }
  return { byNickname, bySecUid, byPeerUserId };
}


export function resolveTargetMapping(lookup, target, identity = {}) {
  if (identity.ambiguous) {
    return { mapping: null, reason: "ambiguous_target" };
  }
  const secUid = String(identity.secUid || "").trim();
  if (secUid) {
    const mapping = lookup.bySecUid.get(secUid);
    return {
      mapping: mapping || null,
      reason: mapping ? "stable_sec_uid" : "stable_identity_not_found",
    };
  }
  const peerUserId = String(identity.peerUserId || "").trim();
  if (peerUserId) {
    const mapping = lookup.byPeerUserId.get(peerUserId);
    return {
      mapping: mapping || null,
      reason: mapping ? "stable_peer_user_id" : "stable_identity_not_found",
    };
  }

  const nickname = normalizeNickname(target);
  const mapping = lookup.byNickname.get(nickname);
  if (mapping) {
    return { mapping, reason: "unique_nickname" };
  }
  return {
    mapping: null,
    reason: lookup.byNickname.has(nickname)
      ? "ambiguous_target"
      : "conversation_not_found",
  };
}

export async function sendMessages({
  client,
  cacheEntries,
  messagesByTarget,
  targetIdentities,
  dryRun,
  cookieString,
  cookieMap,
  sendStrategy,
  identityOverride = null,
}) {
  if (!dryRun) {
    const identity = identityOverride
      || await fetchIdentitySecurityToken(cookieString, cookieMap);
    client.updateSendMessageHeaders({
      identity_security_token: identity.identitySecurityHeader,
      identity_security_device_id: identity.realDeviceId,
      identity_security_aid: "2906",
    });
  }

  const lookup = buildTargetLookup(cacheEntries);
  const resolved = [];
  const unresolved = [];
  const sent = [];
  const normalizedStrategy = normalizeSendStrategy(sendStrategy);

  for (const [target, message] of Object.entries(messagesByTarget)) {
    try {
      const { mapping, reason } = resolveTargetMapping(
        lookup,
        target,
        targetIdentities?.[target] || {},
      );
      if (!mapping) {
        unresolved.push({ target, reason });
        continue;
      }

      const conversation = client.getConversation({
        conversationId: mapping.conversationId,
      });
      if (!conversation) {
        unresolved.push({ target, reason: "conversation_not_loaded", mapping });
        continue;
      }

      resolved.push({
        target,
        nickname: mapping.nickname,
        peerUserId: mapping.peerUserId,
        conversationId: mapping.conversationId,
        conversationShortId: mapping.conversationShortId,
      });

      let delayBeforeSendSeconds = 0;
      if (!dryRun && sent.length > 0 && normalizedStrategy.messageIntervalSecondsMax > 0) {
        delayBeforeSendSeconds = randomBetweenInclusive(
          normalizedStrategy.messageIntervalSecondsMin,
          normalizedStrategy.messageIntervalSecondsMax,
        );
        if (delayBeforeSendSeconds > 0) {
          await sleep(delayBeforeSendSeconds * 1000);
        }
      }

      const payload = JSON.stringify({ text: message, aweType: 774 });
      const messageObject = await client.createMessage({
        type: 7,
        content: payload,
        conversation,
        insert: false,
      });

      if (dryRun) {
        sent.push({
          target,
          dryRun: true,
          message,
          payload,
          conversationId: mapping.conversationId,
          delayBeforeSendSeconds,
        });
        continue;
      }

      const sendResult = await client.sendMessage({ message: messageObject });
      const statusCode = sendResult?.statusCode ?? null;
      sent.push({
        target,
        dryRun: false,
        message,
        success: isSuccessfulSendResult(sendResult),
        statusCode,
        statusName: sendMessageStatusName(statusCode),
        statusMsg: sendResult?.statusMsg ?? "",
        sendResultSummary: publicSendResultSummary(sendResult),
        conversationId: mapping.conversationId,
        delayBeforeSendSeconds,
        sentAt: stableNow(),
      });
    } catch (error) {
      sent.push({
        target,
        dryRun: false,
        message,
        success: false,
        statusCode: null,
        statusName: "exception",
        statusMsg: String(error?.message || error || "protocol target failed"),
        sendResultSummary: { rawKeys: [] },
        conversationId: "",
        delayBeforeSendSeconds: 0,
        sentAt: stableNow(),
      });
    }
  }

  return { resolved, unresolved, sent };
}

// 进程启动时刻：用于测「main 之前」的 Node 启动 + 模块加载耗时
const PROCESS_START_MS = Date.now();

async function main() {
  const bootstrapMs = Date.now() - PROCESS_START_MS;
  const payload = await readStdinJson();
  activeSite = resolveSite(payload);
  const repoRoot = payload.repoRoot || process.cwd();
  const bundleDir = path.join(repoRoot, ".im_sdk_cache");
  // 阶段耗时（诊断「拉好友/读火花慢」用）：随结果一起回传给后端
  const timings = {};
  let lapMark = Date.now();
  const lap = (key) => {
    timings[key] = Math.round(Date.now() - lapMark);
    lapMark = Date.now();
  };
  timings.bootstrap = bootstrapMs;
  timings.stdin = Math.max(0, Date.now() - PROCESS_START_MS - bootstrapMs);
  await ensureBundles(bundleDir);
  lap("bundles");

  const account = payload.account || {};
  const cookieString = toCookieString(account.cookies);
  const cookieMap = extractCookieMap(account.cookies);
  // 出口代理：先于身份请求生效，SDK 内部请求同样走 context.fetch
  activeFetch = buildProxyFetch(account.proxy || null);
  // 身份来源：creator 走 user_token 接口；主站由后端传入（浏览器已解析的 uid）
  let userId = String(account.userId || "").trim();
  if (!userId && activeSite.needIdentityApi) {
    ({ userId } = await fetchSessionIdentity(cookieString, cookieMap));
  }
  if (!userId) {
    throw new ProtocolError("缺少 userId（主站链路需要账号已解析的 user_id）", {
      site: activeSite.key,
    });
  }
  // 注入 IM 会话 token：creator 缺失时发送会被服务端安全校验拦（CheckMessageNotPass）；
  // 主站走 cookie 会话鉴权（SESSION_AUTH），不取 token。
  let imToken = "";
  if (activeSite.needImToken) {
    try {
      imToken = await fetchImToken(cookieString, cookieMap);
    } catch (err) {
      console.error("[im-token] fetch failed:", String(err?.message || err));
    }
  }
  const { client } = await createProtocolClient({
    bundleDir,
    cookieString,
    cookieMap,
    userId,
    imToken,
  });
  lap("sdk_init");

  try {
    // mode = spark_state：只拉会话列表并解析连续聊天（火花）状态，不发送任何消息
    if (String(payload.mode || "send") === "spark_state") {
      const sparks = await collectSparkStates({
        client,
        selfUserId: userId,
        cookieString,
        existingCache: account.protocol_targets_cache || [],
      });
      lap("collect");
      const pendingToday = sparks.filter(
        (item) => item.spark && [2, 3].includes(Number(item.spark.state)),
      ).length;
      console.log(
        JSON.stringify(
          {
            ok: true,
            mode: "spark_state",
            username: account.username || "",
            userId,
            count: sparks.length,
            pendingToday,
            sparks,
            timings,
          },
          null,
          2,
        ),
      );
      return;
    }

    // mode = list_friends：只拉会话列表（= 可私信好友），不发送任何消息
    if (String(payload.mode || "send") === "list_friends") {
      const friends = await collectFriends({
        client,
        selfUserId: userId,
        cookieString,
        existingCache: account.protocol_targets_cache || [],
      });
      lap("collect");
      console.log(
        JSON.stringify(
          {
            ok: true,
            mode: "list_friends",
            username: account.username || "",
            userId,
            count: friends.length,
            sendableCount: friends.filter((item) => item.nickname).length,
            friends,
            protocol_targets_cache: mergeConversationCache(
              account.protocol_targets_cache || [],
              friends,
            ),
            timings,
          },
          null,
          2,
        ),
      );
      return;
    }

    // mode = friends_and_sparks：一次采样同时拿好友列表与火花状态（省一次 SDK 初始化）
    if (String(payload.mode || "send") === "friends_and_sparks") {
      const { friends, sparks } = await collectFriendsAndSparks({
        client,
        selfUserId: userId,
        cookieString,
        existingCache: account.protocol_targets_cache || [],
      });
      lap("collect");
      const pendingToday = sparks.filter(
        (item) => item.spark && [2, 3].includes(Number(item.spark.state)),
      ).length;
      console.log(
        JSON.stringify(
          {
            ok: true,
            mode: "friends_and_sparks",
            username: account.username || "",
            userId,
            count: friends.length,
            sendableCount: friends.filter((item) => item.nickname).length,
            friends,
            sparkCount: sparks.length,
            pendingToday,
            sparks,
            // 好友列表照常并入发送会话缓存（火花不改缓存）
            protocol_targets_cache: mergeConversationCache(
              account.protocol_targets_cache || [],
              friends,
            ),
            timings,
          },
          null,
          2,
        ),
      );
      return;
    }

    const cacheEntries = await buildConversationCache({
      client,
      selfUserId: userId,
      cookieString,
      existingCache: account.protocol_targets_cache || [],
      targetNames: Object.keys(payload.messagesByTarget || {}),
    });
    const execution = await sendMessages({
      client,
      cacheEntries,
      messagesByTarget: payload.messagesByTarget || {},
      targetIdentities: payload.targetIdentities || {},
      dryRun: Boolean(payload.dryRun),
      cookieString,
      cookieMap,
      sendStrategy: payload.sendStrategy || {},
    });

    console.log(
      JSON.stringify(
        {
          ok: true,
          username: account.username || "",
          userId,
          dryRun: Boolean(payload.dryRun),
          protocol_targets_cache: cacheEntries,
          ...execution,
        },
        null,
        2,
      ),
    );
  } finally {
    // 诊断：「拉好友/读火花」总耗时约 42s，但阶段计时只到 ~22s，
    // 差额怀疑花在 SDK 关闭上——这里单独计时（走 stderr，不污染 stdout JSON）
    const disposeStart = Date.now();
    await client.dispose();
    console.error(
      `[timings] dispose=${Date.now() - disposeStart}ms uptime=${Math.round(process.uptime() * 1000)}ms`,
    );
  }
}

const isMain =
  Boolean(process.argv[1]) &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;

/**
 * 刷完 stdout 再退出。
 *
 * 2026-10-14 实测：拉好友/读火花外部耗时 42s，但 Node 自身 uptime 只有 ~22s
 * —— 结果输出后进程还空转约 20s（IM SDK 残留的定时器/连接句柄让事件循环无法
 * 自然结束）。stdout 对管道是异步写入，直接 process.exit 可能截断结果，所以
 * 先排一个空写等它的回调（同一流内有序）再退出。
 */
function finish(code) {
  try {
    process.stdout.write("", () => process.exit(code));
  } catch {
    process.exit(code);
  }
  // 兜底：极端情况下回调不触发，1s 后强制退出
  setTimeout(() => process.exit(code), 1000).unref();
}

if (isMain) {
  main().then(() => finish(0)).catch((error) => {
    console.log(
      JSON.stringify(
        {
          ok: false,
          error: error?.message || String(error),
          details: error?.details || {},
          stack: error?.stack || "",
        },
        null,
        2,
      ),
    );
    finish(1);
  });
}
