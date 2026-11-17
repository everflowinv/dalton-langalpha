#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import http from "node:http";
import { homedir } from "node:os";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const VERSION = "1.1.0";
const API_BASE = "https://www.alphaengine.top/api/v1/kmpsummary";
const HOST = process.env.ALPHAENGINE_MCP_HOST || "127.0.0.1";
const PORT = Number(process.env.ALPHAENGINE_MCP_PORT || 8950);
const WORKSPACE = process.env.DALTON_RESEARCH_HOME || process.env.DALTON_OCW_ROOT || process.env.OPENCLAW_WORKSPACE || path.join(homedir(), ".openclaw", "workspace"); // 兼容旧服务默认目录
const MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024;
const CACHE_TTL_MS = 10 * 60 * 1000;
const SEARCH_CURSOR_TTL_MS = 15 * 60 * 1000;

const CATEGORY_DEFS = {
  sell_side: {
    label: "卖方观点（点评+境内外研报）",
    codes: ["comment", "chinaReport", "foreignReport"],
  },
  sell_side_comment: {
    label: "卖方点评",
    codes: ["comment"],
  },
  sell_side_report: {
    label: "卖方研报（境内+海外）",
    codes: ["chinaReport", "foreignReport"],
  },
  domestic_report: {
    label: "国内研报",
    codes: ["chinaReport"],
  },
  foreign_report: {
    label: "海外研报",
    codes: ["foreignReport"],
  },
  meeting_minutes: {
    label: "会议纪要",
    codes: ["summary"],
  },
  institution_meetings: { label: "本机构录制会议", codes: ["institutionMeeting"] },
  personal_meetings: { label: "个人录制会议", codes: ["personalMeeting"] },
  third_party_reports: { label: "三方报告", codes: ["thirdReport"] },
  expert_library: { label: "专家库（独立权限/额度）", codes: ["expert_summary"] },
  global_think_tank: { label: "全球智库", codes: ["globalThinkTank"] },
  announcement: {
    label: "公司公告",
    codes: ["companyAllAnnouncement"],
  },
  news: {
    label: "资讯",
    codes: ["news"],
  },
};

const SOURCE_ALIASES = {
  摩根士丹利: "Morgan Stanley",
  摩根大通: "J.P.Morgan",
  高盛: "Goldman Sachs",
  花旗: "Citi",
  瑞银: "UBS",
  瑞信: "Credit Suisse",
  美银: "Bank of America",
  德银: "Deutsche Bank",
  巴克莱: "Barclays",
  麦格理: "Macquarie",
};

const cache = new Map();
const searchCursorStates = new Map();

function cached(key) {
  const entry = cache.get(key);
  return entry && Date.now() - entry.time < CACHE_TTL_MS ? entry.value : undefined;
}

function setCached(key, value) {
  cache.set(key, { time: Date.now(), value });
  return value;
}

function decodeJsonBase64Url(value) {
  return JSON.parse(Buffer.from(value, "base64url").toString("utf8"));
}

function jwtPayload(token) {
  try {
    return decodeJsonBase64Url(token.split(".")[1]);
  } catch {
    return {};
  }
}

// Token 只随 webview 子进程的 additionalArguments 下发，主窗口 renderer 上没有。
// App 里没有打开任何模块页时，ps 里一个 --alphaclaw-token= 都不会有，此时登录态其实是好的。
const APP_EXECUTABLE = "/Applications/AlphaEngine.app/Contents/MacOS/AlphaEngine";

function desktopProcessSnapshot() {
  const commands = execFileSync("ps", ["-axo", "command="], { encoding: "utf8" });
  let appRunning = false;
  const candidates = [];
  for (const command of commands.split("\n")) {
    if (command.startsWith(APP_EXECUTABLE)) appRunning = true;
    if (!command.includes("AlphaEngine Helper (Renderer)")) continue;
    const match = command.match(/--alphaclaw-token=([^ ]+)/);
    if (!match) continue;
    try {
      const bundle = decodeJsonBase64Url(match[1]);
      if (!bundle.token) continue;
      const payload = jwtPayload(bundle.token);
      candidates.push({ token: bundle.token, exp: Number(payload.exp || 0), userId: bundle.user_id });
    } catch {
      // Ignore stale or malformed renderer processes.
    }
  }
  candidates.sort((a, b) => b.exp - a.exp);
  return { appRunning, candidates };
}

function desktopTokenCandidates() {
  return desktopProcessSnapshot().candidates;
}

const DESKTOP_STATUS = {
  ready: {
    status: "ready",
    hint: "AlphaEngine Desktop 已就绪。",
  },
  app_not_running: {
    status: "app_not_running",
    error: "E_DESKTOP_NOT_RUNNING: AlphaEngine Desktop 未运行",
    hint: "启动 AlphaEngine，然后在 App 里打开任意模块页并保持开着。",
  },
  no_module_page: {
    status: "no_module_page",
    error: "E_DESKTOP_NO_MODULE_PAGE: AlphaEngine Desktop 在运行，但没有打开的模块页，读不到 token",
    hint: "登录态不用动。在 App 里打开任意会加载网页内容的模块页并保持开着，token 只随模块页的子进程下发。",
  },
  token_expired: {
    status: "token_expired",
    error: "E_DESKTOP_TOKEN_EXPIRED: AlphaEngine 登录态已过期",
    hint: "在 App 内重新登录，再打开一个模块页。",
  },
};

function desktopStatus() {
  const { appRunning, candidates } = desktopProcessSnapshot();
  const now = Math.floor(Date.now() / 1000);
  const valid = candidates.find((candidate) => candidate.exp > now + 30);
  if (valid) return { ...DESKTOP_STATUS.ready, appRunning, candidates, valid, now };
  const key = !appRunning ? "app_not_running" : candidates.length === 0 ? "no_module_page" : "token_expired";
  return { ...DESKTOP_STATUS[key], appRunning, candidates, valid: null, now };
}

function resolveDesktopToken() {
  const state = desktopStatus();
  if (!state.valid) throw new Error(`${state.error}。${state.hint}`);
  return state.valid;
}

async function apiRequest(apiPath, options = {}) {
  const { token } = resolveDesktopToken();
  // Additional fixed, read-only dictionary endpoints live under kmpadmin/kmpstock.
  const url = apiPath.startsWith("/api/v1/") ? `https://www.alphaengine.top${apiPath}` : `${API_BASE}${apiPath}`;
  const response = await fetch(url, {
    ...options,
    headers: {
      Accept: "application/json, text/plain, */*",
      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
      Authorization: `Bearer ${token}`,
      Origin: "https://www.alphaengine.top",
      Referer: "https://www.alphaengine.top/",
      "User-Agent": `AlphaEngine-MCP/${VERSION}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
    signal: options.signal || AbortSignal.timeout(120000),
  });
  const responseText = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(responseText);
  } catch {
    throw new Error(`E_UPSTREAM_RESPONSE: HTTP ${response.status}, non-JSON response`);
  }
  if (!response.ok) {
    throw new Error(`E_UPSTREAM_HTTP: HTTP ${response.status}: ${JSON.stringify(parsed).slice(0, 600)}`);
  }
  if (parsed?.success === false || (parsed?.code && ![0, 200, "0", "200"].includes(parsed.code))) {
    throw new Error(
      `E_UPSTREAM_API: ${parsed.message || parsed.msg || JSON.stringify(parsed).slice(0, 600)}`,
    );
  }
  return parsed?.data ?? parsed?.body ?? parsed;
}

function requireDatePair(dateFrom, dateTo) {
  if (Boolean(dateFrom) !== Boolean(dateTo)) {
    throw new Error("E_BAD_DATE_RANGE: date_from 和 date_to 必须成对提供");
  }
  for (const value of [dateFrom, dateTo]) {
    if (value && (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(`${value}T00:00:00Z`)) || new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) !== value)) {
      throw new Error(`E_BAD_DATE: ${value}，格式应为 YYYY-MM-DD`);
    }
  }
  if (dateFrom && dateFrom > dateTo) throw new Error("E_BAD_DATE_RANGE: date_from 不能晚于 date_to");
}

function dateToMillis(value, endOfDay = false) {
  if (!value) return null;
  const suffix = endOfDay ? "T23:59:59.999+08:00" : "T00:00:00.000+08:00";
  return Date.parse(`${value}${suffix}`);
}

function normalizeText(value) {
  return String(value || "")
    .normalize("NFKC")
    .trim()
    .toLocaleLowerCase("en-US")
    .replace(/\s+/g, " ");
}

function asArray(value) {
  if (value === undefined || value === null || value === "") return [];
  return Array.isArray(value) ? value : [value];
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function truncate(value, maxChars) {
  if (typeof value !== "string") return value;
  return value.length > maxChars ? `${value.slice(0, maxChars)}…` : value;
}

function cleanValue(value, maxChars = 3000) {
  if (typeof value !== "string") return value;
  if (value.startsWith("data:image/") || value.length > 100000) return undefined;
  return truncate(value, maxChars);
}

function flattenInstitutions(data) {
  const institutions = [];
  if (Array.isArray(data?.focus)) institutions.push(...data.focus);
  for (const group of data?.general || []) {
    if (Array.isArray(group?.institution)) institutions.push(...group.institution);
  }
  const byId = new Map();
  for (const item of institutions) {
    const id = item?.institution_id || item?.id;
    const name = item?.institution_name || item?.name;
    if (id && name) byId.set(id, { institution_id: id, institution_name: name });
  }
  return [...byId.values()];
}

async function institutionCatalog(request = apiRequest) {
  const previous = cached("institutions");
  if (previous) return previous;
  return setCached("institutions", flattenInstitutions(await request("/summary/institution/list")));
}

async function resolveSourceNames(sourceNames, request = apiRequest) {
  const requested = unique(asArray(sourceNames).map(String));
  if (!requested.length) return { ids: [], names: [] };
  const catalog = await institutionCatalog(request);
  const ids = [];
  const names = [];
  for (const requestedName of requested) {
    const canonicalName = SOURCE_ALIASES[requestedName] || requestedName;
    const normalized = normalizeText(canonicalName);
    const exact = catalog.filter((item) => normalizeText(item.institution_name) === normalized);
    if (exact.length) {
      exact.forEach((item) => ids.push(item.institution_id));
      names.push(...exact.map((item) => item.institution_name));
      continue;
    }
    const partial = catalog.filter((item) => normalizeText(item.institution_name).includes(normalized));
    if (partial.length === 1) {
      ids.push(partial[0].institution_id);
      names.push(partial[0].institution_name);
      continue;
    }
    const hint = partial.slice(0, 8).map((item) => item.institution_name);
    throw new Error(
      `E_SOURCE_NOT_RESOLVED: 无法唯一匹配来源“${requestedName}”` +
        (hint.length ? `；候选：${hint.join("、")}` : "；可先调用 list_filters(filter_type=sources)"),
    );
  }
  return { ids: unique(ids), names: unique(names) };
}

function flattenTypeTree(nodes, depth = 0, output = []) {
  for (const node of nodes || []) {
    output.push({
      type_id: node.type_id,
      type_name: node.type_name,
      type_name_en: node.type_name_en,
      parent_type_id: node.parent_type_id,
      depth,
    });
    flattenTypeTree(node.child, depth + 1, output);
  }
  return output;
}

async function typeCatalog(code, request = apiRequest) {
  const safeCode = String(code || "composite");
  if (!/^[A-Za-z0-9_-]+$/.test(safeCode)) throw new Error("E_BAD_CODE: 非法文档 code");
  const key = `types:${safeCode}`;
  const previous = cached(key);
  if (previous) return previous;
  const tree = await request(`/summary/type/list/${encodeURIComponent(safeCode)}`);
  return setCached(key, flattenTypeTree(tree));
}

function expandDocumentCodes(categories, rawCodes) {
  const codes = [...asArray(rawCodes).map(String)];
  for (const category of asArray(categories).map(String)) {
    const definition = CATEGORY_DEFS[category];
    if (!definition) {
      throw new Error(
        `E_BAD_CATEGORY: ${category}；可选值：${Object.keys(CATEGORY_DEFS).join(", ")}`,
      );
    }
    codes.push(...definition.codes);
  }
  return unique(codes);
}

function parseCursor(cursor) {
  if (cursor === undefined || cursor === null || cursor === "") {
    return { localToken: null, upstreamCursor: null };
  }
  if (Array.isArray(cursor)) return { localToken: null, upstreamCursor: cursor };
  if (typeof cursor === "string") {
    if (cursor.startsWith("ae1:")) return { localToken: cursor, upstreamCursor: null };
    try {
      const parsed = JSON.parse(cursor);
      if (Array.isArray(parsed)) return { localToken: null, upstreamCursor: parsed };
    } catch {
      // handled below
    }
  }
  throw new Error("E_BAD_CURSOR: cursor 应为 search_library 返回的字符串，或兼容的上游数组游标");
}

function pruneSearchCursorStates() {
  const now = Date.now();
  for (const [token, state] of searchCursorStates) {
    if (now - state.createdAt > SEARCH_CURSOR_TTL_MS) searchCursorStates.delete(token);
  }
}

function saveSearchCursorState(state) {
  pruneSearchCursorStates();
  const token = `ae1:${randomUUID()}`;
  searchCursorStates.set(token, { ...state, createdAt: Date.now() });
  return token;
}

function takeSearchCursorState(token, signature) {
  pruneSearchCursorStates();
  const state = searchCursorStates.get(token);
  if (!state) throw new Error("E_CURSOR_EXPIRED: 搜索游标不存在或已超过 15 分钟，请重新检索");
  if (state.signature !== signature) {
    throw new Error("E_CURSOR_FILTER_MISMATCH: 翻页时 query 与筛选条件必须保持不变");
  }
  return state;
}

function searchSignature(filters) {
  return createHash("sha256").update(JSON.stringify(filters)).digest("hex");
}

function itemTimeMillis(item) {
  const value = item?.rank_date || item?.publish_time;
  if (!value) return 0;
  const normalized = String(value).includes("T") ? String(value) : String(value).replace(" ", "T");
  const withZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(normalized) ? normalized : `${normalized}+08:00`;
  return Date.parse(withZone) || 0;
}

function itemSources(item) {
  return unique(
    asArray(item?.institution_name || item?.institution)
      .flatMap((value) => (typeof value === "string" ? [value] : [])),
  );
}

function itemMarkets(item) {
  const explicit = asArray(item?.market_list).map(String);
  const companyList = asArray(item?.company_list)
    .map((company) => company?.market || company?.market_type)
    .filter(Boolean);
  return unique([...explicit, ...companyList]);
}

function sourceAttribution(item) {
  const source = itemSources(item).join(" / ") || "AlphaEngine";
  const date = String(item?.publish_time || item?.rank_date || "").slice(0, 10) || "日期未知";
  const title = item?.title || item?.search_title || "未命名文档";
  const docId = item?.doc_id || item?.id || item?.document_id || "unknown";
  return {
    markdown: `**${source}**, “${title}” (${date}; AlphaEngine doc_id: ${docId})`,
  };
}

function compactSearchResult(item, includeSnippet) {
  const result = {
    doc_id: item?.doc_id || item?.id || item?.document_id,
    title: item?.title || item?.search_title,
    publish_time: item?.publish_time,
    rank_date: item?.rank_date,
    sources: itemSources(item),
    document_code: item?.code,
    type_id: item?.type_id || item?.document_type_id,
    type_name: item?.type_name || item?.type_show_name || item?.type_full_name,
    publish_date: String(item?.publish_time || "").slice(0, 10),
    companies: unique(asArray(item?.company_name).map(String)),
    industries: unique(asArray(item?.industry_name).map(String)),
    markets: itemMarkets(item),
    source_attribution: sourceAttribution(item),
  };
  // Keep provenance and classification separate. An institutional meeting still
  // returns code=summary; institution_name describes the broker, not its owner.
  for (const key of ["summary_id", "title_cn", "institution_name", "institution_id", "document_type_id",
    "document_type_name", "type_show_name", "type_full_id", "type_full_name", "first_type_name",
    "institution_meeting_flag", "personal_meeting_flag", "owner_institution_id", "belong_wechat_group",
    "appointment_person_id", "doc_icon", "page_num", "author_list", "company_code", "company_show_name",
    "market_list", "rank_date", "read", "detail", "state", "realtime", "sensitive_permission", "label"] ) {
    if (Object.hasOwn(item, key)) result[key] = item[key];
  }
  if (includeSnippet !== false) {
    result.snippet = truncate(item?.doc_introduce || item?.digest || "", 1800);
  }
  return Object.fromEntries(
    Object.entries(result).filter(([, value]) => value !== undefined && value !== ""),
  );
}

function resultMatches(item, filters) {
  const publishDate = String(item?.publish_time || "").slice(0, 10);
  if (filters.dateFrom && publishDate && publishDate < filters.dateFrom) return false;
  if (filters.dateTo && publishDate && publishDate > filters.dateTo) return false;
  if (filters.scope === "institution" && item?.institution_meeting_flag !== true) return false;
  if (filters.scope === "personal" && item?.personal_meeting_flag !== true) return false;
  if (filters.codes.length && !filters.codes.includes("composite") && !filters.codes.includes(item?.code)) return false;
  if (filters.typeIds.length) {
    const typeId = item?.type_id || item?.document_type_id;
    const ancestors = String(item?.type_full_id || "").split(":");
    if (!filters.typeIds.some((id) => id === typeId || ancestors.includes(id))) return false;
  }
  if (filters.sourceNames.length) {
    const sources = itemSources(item).map(normalizeText);
    if (!filters.sourceNames.some((name) => sources.includes(normalizeText(name)))) return false;
  }
  if (filters.markets.length) {
    const markets = itemMarkets(item);
    if (markets.length && !filters.markets.some((market) => markets.includes(market))) return false;
  }
  return true;
}

async function searchLibrary(args, request = apiRequest) {
  const query = String(args.query || "").trim();
  requireDatePair(args.date_from, args.date_to);
  const limit = Math.max(1, Math.min(Number(args.limit || 10), 50));
  const typeIds = unique(asArray(args.type_ids).map(String));
  const requestedCodes = expandDocumentCodes(args.document_categories, args.document_codes);
  const scopeCodes = { institutionMeeting: "institution", personalMeeting: "personal" };
  const requestedScopes = unique([args.meeting_scope === "all" ? null : args.meeting_scope,
    ...requestedCodes.map((code) => scopeCodes[code])]);
  if (requestedScopes.length > 1) throw new Error("E_SCOPE_CONFLICT: 机构与个人会议请分别检索");
  const scope = requestedScopes[0] || "all";
  if (!["all", "institution", "personal"].includes(scope)) throw new Error("E_BAD_SCOPE: meeting_scope 应为 all、institution 或 personal");
  let codes = requestedCodes.filter((code) => !scopeCodes[code]);
  if (scope !== "all" && codes.some((code) => !["summary", "composite"].includes(code))) {
    throw new Error("E_SCOPE_CONFLICT: 机构/个人会议范围不能与非会议文档类别混用");
  }
  if (scope !== "all") codes = ["summary"];
  const scopeCode = Object.keys(scopeCodes).find((code) => scopeCodes[code] === scope);
  const sourceResolution = await resolveSourceNames(args.source_names, request);
  const sourceIds = unique([...sourceResolution.ids, ...asArray(args.source_ids).map(String)]);
  const sourceNames = sourceResolution.names;
  const markets = unique(asArray(args.markets).map(String));
  const effectiveCode = scopeCode || (codes.length === 1 ? codes[0] : "composite");
  const requestCodes = scopeCode ? [scopeCode] : codes;
  const documentTypeList = requestCodes.length
    ? requestCodes.map((code) => ({ code, type_ids: typeIds }))
    : [{ code: "composite", type_ids: typeIds }];
  const sort = args.sort || (query ? "relevance" : "newest");
  if (query && sort === "newest") throw new Error("E_QUERY_SORT: 有关键词的主题检索请用 sort=relevance；无关键词列表可用 newest");
  if (!["relevance", "newest"].includes(sort)) throw new Error("E_BAD_SORT: sort 应为 relevance 或 newest");
  const orderBy = sort === "newest" ? "-time" : "comprehensive";
  const filters = {
    query,
    date_from: args.date_from || null,
    date_to: args.date_to || null,
    source_names: sourceNames,
    source_ids: sourceIds,
    document_categories: asArray(args.document_categories),
    document_codes: codes,
    type_ids: typeIds,
    markets,
    industry_names: unique(asArray(args.industry_names).map(String)),
    optional_only: Boolean(args.optional_only),
    meeting_scope: scope,
    search_in: args.search_in || "full_text",
    company_names: unique(asArray(args.company_names).map(String)),
    company_short_names: unique(asArray(args.company_short_names).map(String)),
    stock_group_ids: unique(asArray(args.stock_group_ids).map(String)),
    first_regions: unique(asArray(args.first_regions).map(String)),
    second_regions: unique(asArray(args.second_regions).map(String)),
    own_reservations_only: Boolean(args.own_reservations_only),
    sort,
  };
  if (!["full_text", "title"].includes(filters.search_in)) throw new Error("E_BAD_SEARCH_IN: search_in 应为 full_text 或 title");
  if (filters.own_reservations_only && scope !== "institution") throw new Error("E_BAD_SCOPE: own_reservations_only 仅用于机构会议");
  const signature = searchSignature(filters);
  const parsedCursor = parseCursor(args.cursor);
  let cursor = parsedCursor.upstreamCursor;
  let upstreamTotal = null;
  let upstreamHasMore = false;
  let buffer = [];
  let seen = new Set();
  let mustFetchInitialPage = true;
  let distinctIds = [];
  let upstreamPages = 0;

  if (parsedCursor.localToken) {
    const state = takeSearchCursorState(parsedCursor.localToken, signature);
    cursor = state.upstreamCursor;
    upstreamTotal = state.upstreamTotal;
    upstreamHasMore = state.upstreamHasMore;
    buffer = [...state.pending];
    seen = new Set(state.seen);
    distinctIds = state.distinctIds || [];
    mustFetchInitialPage = false;
  }

  for (let page = 0; page < 5 && buffer.length < limit; page += 1) {
    if (!mustFetchInitialPage && (!upstreamHasMore || !cursor)) break;
    const payload = {
      query,
      code: effectiveCode,
      type_id: typeIds,
      institution_id: sourceIds,
      doc_type_list: documentTypeList,
      industry_name: unique(asArray(args.industry_names).map(String)),
      query_type: filters.search_in === "title" ? "1" : "2",
      company_short_name: filters.company_short_names,
      company_name: filters.company_names,
      market_list: markets,
      stock_group_ids: filters.stock_group_ids,
      is_optional: Boolean(args.optional_only),
      start_time: dateToMillis(args.date_from, false),
      end_time: dateToMillis(args.date_to, true),
      size: Math.max(limit, 20),
      search_after: cursor,
      order_by: orderBy,
      distinct_ids: distinctIds,
      first_region: filters.first_regions,
      second_region: filters.second_regions,
      ...(filters.own_reservations_only ? { own_subscribe_flag: true } : {}),
    };
    const data = await request("/summary/search/list/new", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    upstreamPages += 1;
    const results = data?.results || data?.list || (Array.isArray(data) ? data : []);
    upstreamTotal ??= data?.total ?? results.length;
    for (const item of results) {
      const docId = item?.doc_id || item?.id || item?.document_id;
      if (docId && seen.has(docId)) continue;
      if (!resultMatches(item, {
        dateFrom: args.date_from,
        dateTo: args.date_to,
        codes,
        typeIds,
        sourceNames,
        markets,
        scope,
      })) continue;
      if (docId) seen.add(docId);
      buffer.push(item);
    }
    const nextUpstreamCursor = data?.search_after ?? data?.searchAfter ?? null;
    upstreamHasMore = Boolean(data?.has_next_page ?? data?.hasNextPage);
    if (upstreamHasMore && (!nextUpstreamCursor || JSON.stringify(nextUpstreamCursor) === JSON.stringify(cursor))) {
      throw new Error("E_UPSTREAM_CURSOR: 上游声明还有数据但未推进游标；不能把部分结果当作全量");
    }
    cursor = nextUpstreamCursor;
    distinctIds = results.map((item) => item.id || item.doc_id).filter(Boolean);
    mustFetchInitialPage = false;
    if (!upstreamHasMore || !cursor) break;
  }

  // Keep upstream rank ordering and pagination boundaries intact. Re-sorting a
  // single page by publish_time can move newer institutional recordings behind old ones.
  const collected = buffer.splice(0, limit);
  const hasMore = buffer.length > 0 || Boolean(upstreamHasMore && cursor);
  const nextCursor = hasMore
    ? saveSearchCursorState({
        signature,
        pending: buffer,
        upstreamCursor: cursor,
        upstreamHasMore,
        upstreamTotal,
        seen: [...seen],
        distinctIds,
      })
    : null;

  return {
    query,
    filters,
    total_upstream: upstreamTotal,
    returned: collected.length,
    results: collected.map((item) => compactSearchResult(item, args.include_snippets)),
    cursor: nextCursor,
    has_more: hasMore,
    upstream_pages_fetched: upstreamPages,
    pagination_note: "继续使用 cursor，直到 has_more=false；total_upstream 是上游匹配数，可能含被本地精确条件过滤的记录。",
    next_step: "对需要深入引用的结果调用 get_document(doc_id, offset, max_chars)。",
  };
}

function selectDocumentText(detail, field = "auto") {
  if (field !== "auto") return typeof detail?.[field] === "string" ? detail[field] : "";
  const candidates = [
    detail?.content,
    detail?.original_text,
    detail?.full_content,
    detail?.text,
    detail?.markdown,
  ];
  return candidates.find((value) => typeof value === "string" && value.length) || "";
}

async function fetchDocumentDetail(docId, mode = "auto") {
  const encoded = encodeURIComponent(docId);
  const cacheKey = `document:${mode}:${docId}`;
  const previous = cached(cacheKey);
  if (previous) return previous;
  if (mode === "private") {
    return setCached(cacheKey, await apiRequest(`/summary/private/detail/${encoded}`));
  }
  if (mode === "public") {
    return setCached(
      cacheKey,
      await apiRequest(`/summary/detail/${encoded}/true?system=false`),
    );
  }
  try {
    return setCached(
      cacheKey,
      await apiRequest(`/summary/detail/${encoded}/true?system=false`),
    );
  } catch (publicError) {
    // A quota/permission/authentication refusal is not a reason to retry through
    // a different document route, and may charge another read.
    if (/额度|权限|登录|token|HTTP (?:401|403|429)/i.test(String(publicError.message))) throw publicError;
    try {
      return setCached(cacheKey, await apiRequest(`/summary/private/detail/${encoded}`));
    } catch {
      throw publicError;
    }
  }
}

function documentMetadata(detail) {
  const metadata = {
    ...compactSearchResult(detail, false),
    doc_id: detail?.doc_id || detail?.id,
    title: detail?.title || detail?.search_title,
    publish_time: detail?.publish_time,
    rank_date: detail?.rank_date,
    sources: itemSources(detail),
    document_code: detail?.code,
    type_id: detail?.type_id || detail?.document_type_id,
    type_name: detail?.type_name || detail?.type_show_name || detail?.type_full_name,
    companies: unique(asArray(detail?.company_name).map(String)),
    industries: unique(asArray(detail?.industry_name).map(String)),
    authors: unique(asArray(detail?.author_list).map((author) =>
      typeof author === "string" ? author : author?.name || author?.author_name,
    )),
    file_format: detail?.file_format,
    page_num: detail?.page_num,
    digest: cleanValue(detail?.digest || detail?.digest_cn, 5000),
    main_point: cleanValue(detail?.main_point, 5000),
    question_answer: cleanValue(detail?.question_answer, 5000),
    attachment_available: Boolean(detail?.file_path || detail?.original_pdf_path || detail?.attachment_path),
    source_attribution: sourceAttribution(detail),
  };
  return Object.fromEntries(
    Object.entries(metadata).filter(([, value]) => value !== undefined && value !== ""),
  );
}

async function getDocument(args) {
  if (!args.doc_id) throw new Error("E_BAD_ARGS: doc_id 必填");
  const mode = args.mode || "auto";
  if (!["auto", "public", "private"].includes(mode)) {
    throw new Error("E_BAD_MODE: mode 应为 auto、public 或 private");
  }
  const contentField = args.content_field || "auto";
  if (!["auto", "original_text", "content", "digest", "digest_cn", "main_point", "question_answer"].includes(contentField)) {
    throw new Error("E_BAD_CONTENT_FIELD: 不支持的正文字段");
  }
  const detail = await fetchDocumentDetail(String(args.doc_id), mode);
  const text = selectDocumentText(detail, contentField);
  const availableFields = Object.fromEntries(["content", "original_text", "full_content", "text", "markdown", "digest", "digest_cn", "main_point", "question_answer"]
    .filter((key) => typeof detail?.[key] === "string" && detail[key].length).map((key) => [key, detail[key].length]));
  const offset = Math.max(0, Number(args.offset || 0));
  const maxChars = Math.max(1, Math.min(Number(args.max_chars || 12000), 30000));
  const textChunk = text.slice(offset, offset + maxChars);
  return {
    metadata: documentMetadata(detail),
    content_field: contentField,
    available_content_fields: availableFields,
    ...(text ? {} : { warning: "所选正文字段为空；请查看 available_content_fields，不能据此认定整篇文档为空。" }),
    content_chars: text.length,
    content_sha256: text ? createHash("sha256").update(text).digest("hex") : null,
    offset,
    returned_chars: textChunk.length,
    text: textChunk,
    next_offset: offset + textChunk.length < text.length ? offset + textChunk.length : null,
    complete: offset === 0 && textChunk.length === text.length,
  };
}

function collectCandidateUrls(value, keyPath = "", output = []) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => collectCandidateUrls(item, `${keyPath}[${index}]`, output));
  } else if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      collectCandidateUrls(child, keyPath ? `${keyPath}.${key}` : key, output);
    }
  } else if (
    typeof value === "string" &&
    /^https?:\/\//i.test(value) &&
    /(file|path|url|download|pdf|attachment)/i.test(keyPath)
  ) {
    output.push({ key: keyPath, url: value });
  }
  return output;
}

function extensionFor(contentType, url, bytes) {
  if (/pdf/i.test(contentType) || bytes.subarray(0, 5).toString("ascii") === "%PDF-") return ".pdf";
  if (/wordprocessingml/i.test(contentType) || /\.docx(?:$|\?)/i.test(url)) return ".docx";
  if (/msword/i.test(contentType) || /\.doc(?:$|\?)/i.test(url)) return ".doc";
  if (/spreadsheetml/i.test(contentType) || /\.xlsx(?:$|\?)/i.test(url)) return ".xlsx";
  if (/ms-excel/i.test(contentType) || /\.xls(?:$|\?)/i.test(url)) return ".xls";
  if (/presentationml/i.test(contentType) || /\.pptx(?:$|\?)/i.test(url)) return ".pptx";
  if (/ms-powerpoint/i.test(contentType) || /\.ppt(?:$|\?)/i.test(url)) return ".ppt";
  if (/text\/plain/i.test(contentType) || /\.txt(?:$|\?)/i.test(url)) return ".txt";
  return ".bin";
}

function documentFileCandidates(value) {
  return collectCandidateUrls(value).filter((item) =>
    /\.(?:pdf|docx?|xlsx?|pptx?|txt)(?:$|\?)/i.test(item.url) ||
    (/original_pdf_path|file_path/i.test(item.key) && !/\.(?:mp[34]|wav|m4a|mov|png|jpe?g|gif|webp)(?:$|\?)/i.test(item.url)));
}

async function attachmentRows(docId, request = apiRequest) {
  const data = await request(`/summary/attachment/list/${encodeURIComponent(docId)}`);
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.list)) return data.list;
  throw new Error("E_UPSTREAM_ATTACHMENTS: 附件列表格式异常");
}

async function listAttachments(args, request = apiRequest) {
  if (!args.doc_id) throw new Error("E_BAD_ARGS: doc_id 必填");
  const rows = await attachmentRows(args.doc_id, request);
  return { doc_id: args.doc_id, returned: rows.length, attachments: rows.map((row, index) => ({
    attachment_index: index, ...safeMetadata(row), downloadable_document: documentFileCandidates(row).length > 0,
  })), next_step: "下载选定文档附件用 download_document(doc_id, attachment_index)；不返回签名地址。" };
}

async function downloadDocument(args) {
  if (!args.doc_id) throw new Error("E_BAD_ARGS: doc_id 必填");
  if (args.attachment_index !== undefined && (!Number.isInteger(args.attachment_index) || args.attachment_index < 0)) throw new Error("E_BAD_ATTACHMENT: attachment_index 须为非负整数");
  const isAttachment = args.attachment_index !== undefined;
  const detail = isAttachment ? (await attachmentRows(String(args.doc_id)))[args.attachment_index]
    : await fetchDocumentDetail(String(args.doc_id), args.mode || "auto");
  if (!detail) throw new Error("E_BAD_ATTACHMENT: 附件索引不存在，请重新 list_attachments");
  const candidates = documentFileCandidates(detail);
  const candidate =
    candidates.find((item) => /original_pdf_path|file_path/i.test(item.key) && /\.pdf(?:$|\?)/i.test(item.url)) ||
    candidates.find((item) => /\.pdf(?:$|\?)/i.test(item.url)) ||
    candidates[0];
  if (!candidate) throw new Error("E_FILE_NOT_FOUND: 文档详情没有可下载的原始文件地址");
  const response = await fetch(candidate.url, {
    headers: { "User-Agent": `AlphaEngine-MCP/${VERSION}` },
    redirect: "follow",
    signal: AbortSignal.timeout(120000),
  });
  if (!response.ok) throw new Error(`E_FILE_HTTP: HTTP ${response.status}`);
  const declaredSize = Number(response.headers.get("content-length") || 0);
  if (declaredSize > MAX_DOWNLOAD_BYTES) {
    throw new Error(`E_FILE_TOO_LARGE: 文件 ${declaredSize} bytes，超过 100MB 上限`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > MAX_DOWNLOAD_BYTES) throw new Error("E_FILE_TOO_LARGE: 文件超过 100MB 上限");
  const contentType = response.headers.get("content-type") || "application/octet-stream";
  const extension = extensionFor(contentType, candidate.url, bytes);
  const safeId = String(args.doc_id).replace(/[^A-Za-z0-9_-]/g, "_");
  const directory = path.resolve(WORKSPACE, "temp/alphaengine-mcp-downloads");
  await mkdir(directory, { recursive: true });
  const outputPath = path.join(directory, `${safeId}${isAttachment ? `-attachment-${args.attachment_index}` : ""}${extension}`);
  await writeFile(outputPath, bytes, { mode: 0o600 });
  return {
    doc_id: String(args.doc_id),
    title: detail?.title,
    local_path: outputPath,
    bytes: bytes.length,
    content_type: contentType,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    valid_pdf_signature: bytes.subarray(0, 5).toString("ascii") === "%PDF-",
    source_attribution: sourceAttribution(detail),
  };
}

function filterPage(items, args, key) {
  const offset = Math.max(0, Number(args.offset || 0));
  const limit = Math.max(1, Math.min(Number(args.limit || 50), 200));
  const selected = items.slice(offset, offset + limit);
  return { [key]: selected, total: items.length, offset, returned: selected.length,
    next_offset: offset + selected.length < items.length ? offset + selected.length : null };
}

function flattenDictionary(nodes, fields, depth = 0, output = []) {
  for (const node of nodes || []) {
    output.push({ ...Object.fromEntries(fields.filter((key) => Object.hasOwn(node, key)).map((key) => [key, node[key]])), depth });
    flattenDictionary(node.child, fields, depth + 1, output);
  }
  return output;
}

async function listFilters(args, request = apiRequest) {
  const filterType = args.filter_type || "categories";
  const requestedQuery = String(args.query || "");
  const query = normalizeText(SOURCE_ALIASES[requestedQuery] || requestedQuery);
  const limit = Math.max(1, Math.min(Number(args.limit || 50), 200));
  if (filterType === "categories") {
    return {
      categories: Object.entries(CATEGORY_DEFS).map(([id, definition]) => ({
        id,
        label: definition.label,
        document_codes: definition.codes,
      })),
    };
  }
  if (filterType === "sources") {
    const catalog = await institutionCatalog(request);
    return filterPage(catalog.filter((item) => !query || normalizeText(`${item.institution_name} ${item.institution_id}`).includes(query)), args, "sources");
  }
  if (filterType === "document_types") {
    const types = await typeCatalog(args.document_code || "composite", request);
    return {
      document_code: args.document_code || "composite",
      ...filterPage(types
        .filter((item) =>
          !query ||
          normalizeText(item.type_name).includes(query) ||
          normalizeText(item.type_name_en).includes(query) ||
          normalizeText(item.type_id).includes(query),
        ), args, "document_types"),
    };
  }
  if (filterType === "upstream_categories") {
    const items = await request("/summary/search/category");
    return filterPage(items.filter((item) => !query || normalizeText(`${item.key} ${item.value} ${item.value_en}`).includes(query)), args, "upstream_categories");
  }
  if (filterType === "industries") {
    const items = await request(`/summary/industry/list?code=${encodeURIComponent(args.document_code || "composite")}`);
    const flat = flattenDictionary(items, ["industry_id", "industry_name", "industry_name_en"]);
    return filterPage(flat.filter((item) => !query || normalizeText(JSON.stringify(item)).includes(query)), args, "industries");
  }
  if (filterType === "companies") {
    if (!requestedQuery.trim()) throw new Error("E_BAD_ARGS: 公司词典须提供 query（公司名称或代码）");
    const items = await request(`/api/v1/kmpadmin/dic/company/list?keyword=${encodeURIComponent(requestedQuery.trim())}`);
    return filterPage(items, args, "companies");
  }
  if (["watchlist_groups", "watchlist_stocks"].includes(filterType)) {
    if (filterType === "watchlist_stocks" && !args.group_id) throw new Error("E_BAD_ARGS: watchlist_stocks 须提供 watchlist_groups 返回的 group_id");
    const apiPath = filterType === "watchlist_groups" ? "/api/v1/kmpstock/stock/self/group/list"
      : `/api/v1/kmpstock/stock/self/stock/list/all/?groupId=${encodeURIComponent(args.group_id)}`;
    const items = await request(apiPath);
    if (!Array.isArray(items)) throw new Error("E_UPSTREAM_RESPONSE: 自选列表格式异常");
    return filterPage(items.filter((item) => !query || normalizeText(JSON.stringify(item)).includes(query)).map(safeMetadata), args, filterType);
  }
  if (filterType === "regions") {
    const flat = flattenDictionary(await request("/region/list"), ["id", "first_region", "second_region", "region_en"]);
    return filterPage(flat.filter((item) => !query || normalizeText(JSON.stringify(item)).includes(query)), args, "regions");
  }
  throw new Error("E_BAD_FILTER_TYPE: 不支持的词典，请查看 list_filters schema");
}

const RECORDING_STATES = { 1: "未开始", 2: "录制中", 3: "录制失败", 4: "转录中", 5: "转录成功" };
function recordingType(args) {
  const scope = args.meeting_scope || "institution";
  if (!["institution", "personal"].includes(scope)) throw new Error("E_BAD_SCOPE: 录制任务须选择 institution 或 personal");
  return scope === "institution" ? "institutionMeeting" : "personalMeeting";
}

// Metadata may contain signed media links or meeting passcodes; do not expose them.
function safeMetadata(value) {
  if (Array.isArray(value)) return value.map(safeMetadata);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !/(token|authorization|cookie|password|passcode|secret|logo)/i.test(key))
    .map(([key, child]) => [key, safeMetadata(child)]));
  if (typeof value === "string" && /^https?:\/\//i.test(value)) return "[link omitted]";
  return value;
}

async function listRecordingTasks(args, request = apiRequest) {
  const type = recordingType(args);
  requireDatePair(args.date_from, args.date_to);
  const page = Math.max(1, Number(args.page || 1));
  const size = Math.max(1, Math.min(Number(args.limit || 20), 100));
  const payload = { type, page_num: page, size, own_subscribe_flag: Boolean(args.own_reservations_only),
    optional_flag: Boolean(args.optional_only), query: String(args.query || ""),
    transcribe_state: args.states || [], institution_id: args.source_ids || [], industry_id: args.industry_ids || [],
    start_time: dateToMillis(args.date_from), end_time: dateToMillis(args.date_to, true) };
  const data = await request("/summary/search/booked/list", { method: "POST", body: JSON.stringify(payload) });
  const rows = (data.results || []).map((row) => ({ ...safeMetadata(row), state_name: RECORDING_STATES[row.transcribe_state] || "未知" }));
  const current = data.current_page_num ?? page;
  // The paged frontend uses total/current_page_num. has_next_page belongs to the
  // separate time-anchor cursor mode and is false even on page 1 of 1,879 tasks.
  const total = data.total;
  const more = Number.isFinite(total) ? current * size < total : rows.length === size;
  if (more && !rows.length) throw new Error("E_UPSTREAM_PAGE: 任务总数还有数据但当前页为空");
  return { meeting_scope: args.meeting_scope || "institution", total, page: current, limit: size, returned: rows.length,
    results: rows, has_more: more, next_page: more ? current + 1 : null,
    state_definitions: RECORDING_STATES,
    note: "录制预约任务（含未开始/失败/处理中），与已入库会议文档数量不同；正文用 search_library 的机构/个人会议范围查找。" };
}

async function getRecordingStatus(args, request = apiRequest) {
  const type = recordingType(args);
  const [counts, preview] = await Promise.all([
    request(`/summary/search/booked/count/list?type=${type}`),
    request("/summary/search/booked/preview", { method: "POST", body: JSON.stringify({ type }) }),
  ]);
  return { meeting_scope: args.meeting_scope || "institution", counts, preview: safeMetadata(preview),
    state_definitions: RECORDING_STATES, note: "counts 保留上游原字段；完整任务用 list_recording_tasks 分页查询。" };
}

async function getRecordingTask(args, request = apiRequest) {
  if (!args.roadshow_id) throw new Error("E_BAD_ARGS: roadshow_id 必填，取自 list_recording_tasks");
  const type = recordingType(args);
  const suffix = args.wechat_group ? `?wechatGroup=${encodeURIComponent(args.wechat_group)}` : "";
  return { meeting_scope: args.meeting_scope || "institution", roadshow_id: args.roadshow_id,
    detail: safeMetadata(await request(`/summary/search/reservation/info/${type}/${encodeURIComponent(args.roadshow_id)}${suffix}`)) };
}

async function getDocumentInfo(args, request = apiRequest) {
  if (!args.doc_id) throw new Error("E_BAD_ARGS: doc_id 必填");
  return { doc_id: args.doc_id, ...safeMetadata(await request(`/summary/doc/base?doc_id=${encodeURIComponent(args.doc_id)}`)),
    note: "仅基础类型/已读状态，不读取全文；完整文本使用 get_document。" };
}

async function authStatus() {
  const { appRunning, candidates, valid, now, status, hint } = desktopStatus();
  return {
    status,
    hint,
    app_running: appRunning,
    desktop_renderer_processes: candidates.length,
    authenticated: Boolean(valid),
    token_expires_at: valid ? new Date(valid.exp * 1000).toISOString() : null,
    token_seconds_remaining: valid ? valid.exp - now : 0,
    api_base: API_BASE,
    note: "Token 仅从运行中的 AlphaEngine Desktop 模块页子进程读取，不落盘、不返回。",
  };
}

const TOOLS = {
  search_library: {
    description:
      "检索 AlphaEngine 投研文档库及本机构/个人录制会议。关键词用 relevance；无关键词列表用 newest。按 cursor 翻到 has_more=false 才算完成；来源券商与录制归属分开。",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "全文关键词，如公司名、产品名或主题" },
        date_from: { type: "string", description: "起始发布日期 YYYY-MM-DD，须与 date_to 同时提供" },
        date_to: { type: "string", description: "结束发布日期 YYYY-MM-DD，须与 date_from 同时提供" },
        source_names: {
          type: "array",
          items: { type: "string" },
          description: "来源机构名称，如 Morgan Stanley、中信证券",
        },
        source_ids: { type: "array", items: { type: "string" } },
        document_categories: {
          type: "array",
          items: { type: "string", enum: Object.keys(CATEGORY_DEFS) },
          description: "友好文档分类；只搜卖方点评使用 sell_side_comment",
        },
        document_codes: {
          type: "array",
          items: { type: "string" },
          description: "AlphaEngine 原始大类 code，如 comment、chinaReport、foreignReport",
        },
        type_ids: {
          type: "array",
          items: { type: "string" },
          description: "细分文档类型 ID；可通过 list_filters(document_types) 查询",
        },
        markets: { type: "array", items: { type: "string", enum: ["A", "HK", "US"] } },
        industry_names: { type: "array", items: { type: "string" } },
        meeting_scope: { type: "string", enum: ["all", "institution", "personal"], default: "all", description: "institution=本机构录制，personal=本人录制；不按券商来源判断归属" },
        search_in: { type: "string", enum: ["full_text", "title"], default: "full_text" },
        company_names: { type: "array", items: { type: "string" }, description: "公司词典 company_name；list_filters(companies) 可按名称或代码解析" },
        company_short_names: { type: "array", items: { type: "string" } },
        stock_group_ids: { type: "array", items: { type: "string" } },
        first_regions: { type: "array", items: { type: "string" } },
        second_regions: { type: "array", items: { type: "string" } },
        own_reservations_only: { type: "boolean", default: false, description: "仅自己预约的机构会议；查整个机构请省略" },
        optional_only: { type: "boolean", default: false },
        sort: { type: "string", enum: ["relevance", "newest"], description: "有 query 默认 relevance；空 query 默认 newest，保留上游 rank_date 顺序" },
        cursor: {
          description:
            "直接传入上次 search_library 返回的 opaque cursor 字符串（ae1:...）；兼容旧版上游数组游标及其 JSON 字符串",
          anyOf: [{ type: "array" }, { type: "string" }, { type: "null" }],
        },
        limit: { type: "integer", minimum: 1, maximum: 50, default: 10 },
        include_snippets: { type: "boolean", default: true },
      },
      additionalProperties: false,
    },
    call: searchLibrary,
  },
  get_document: {
    description:
      "按 doc_id 读取完整文档正文。正文较长时用 next_offset 连续分段读取，content_sha256 用于校验各段属于同一版本。",
    inputSchema: {
      type: "object",
      properties: {
        doc_id: { type: "string" },
        mode: { type: "string", enum: ["auto", "public", "private"], default: "auto" },
        offset: { type: "integer", minimum: 0, default: 0 },
        max_chars: { type: "integer", minimum: 1, maximum: 30000, default: 12000 },
        content_field: { type: "string", enum: ["auto", "original_text", "content", "digest", "digest_cn", "main_point", "question_answer"], default: "auto", description: "正文/转录/摘要/要点/问答可分别分页；auto 保持既有正文优先顺序" },
      },
      required: ["doc_id"],
      additionalProperties: false,
    },
    call: getDocument,
  },
  download_document: {
    description:
      "下载原始 PDF/Office 文档到 OpenClaw workspace 临时目录；不暴露签名 URL。文本点评可能没有附件。",
    inputSchema: {
      type: "object",
      properties: {
        doc_id: { type: "string" },
        mode: { type: "string", enum: ["auto", "public", "private"], default: "auto" },
        attachment_index: { type: "integer", minimum: 0, description: "可选，list_attachments 返回的附件序号；省略则下载原始文档" },
      },
      required: ["doc_id"],
      additionalProperties: false,
    },
    call: downloadDocument,
  },
  list_filters: {
    description: "查询友好分类、当前账户上游分类、来源机构、细分类型、行业、公司、地区和自选股分组/成员。词典用 offset/next_offset 翻页；show 为上游账户可见状态。",
    inputSchema: {
      type: "object",
      properties: {
        filter_type: {
          type: "string",
          enum: ["categories", "upstream_categories", "sources", "document_types", "industries", "companies", "regions", "watchlist_groups", "watchlist_stocks"],
          default: "categories",
        },
        query: { type: "string", description: "名称或 ID 的模糊匹配词" },
        document_code: { type: "string", default: "composite" },
        group_id: { type: "string", description: "watchlist_stocks 所需的分组 ID，来自 watchlist_groups.id；可用于 search_library.stock_group_ids" },
        limit: { type: "integer", minimum: 1, maximum: 200, default: 50 },
        offset: { type: "integer", minimum: 0, default: 0 },
      },
      additionalProperties: false,
    },
    call: listFilters,
  },
  get_document_info: {
    description: "只查文档基础类型和已读状态，不取全文，便于先检查专家库类型再决定是否读取。",
    inputSchema: { type: "object", properties: { doc_id: { type: "string" } }, required: ["doc_id"], additionalProperties: false },
    call: getDocumentInfo,
  },
  list_attachments: {
    description: "列出文档补充附件及下载序号，不暴露签名地址；下载仍走 download_document。",
    inputSchema: { type: "object", properties: { doc_id: { type: "string" } }, required: ["doc_id"], additionalProperties: false },
    call: listAttachments,
  },
  list_recording_tasks: {
    description: "分页列出机构/个人会议预约及录制任务，含未开始、录制中、失败、转录中和成功；不是已入库纪要列表。使用 next_page 翻页。",
    inputSchema: { type: "object", properties: {
      meeting_scope: { type: "string", enum: ["institution", "personal"], default: "institution" },
      page: { type: "integer", minimum: 1, default: 1 },
      limit: { type: "integer", minimum: 1, maximum: 100, default: 20 },
      query: { type: "string" },
      states: { type: "array", items: { type: "integer", enum: [1, 2, 3, 4, 5] }, description: "1未开始/2录制中/3录制失败/4转录中/5转录成功" },
      date_from: { type: "string", description: "会议日期 YYYY-MM-DD，与 date_to 成对" },
      date_to: { type: "string" },
      source_ids: { type: "array", items: { type: "string" } },
      industry_ids: { type: "array", items: { type: "string" } },
      own_reservations_only: { type: "boolean", default: false },
      optional_only: { type: "boolean", default: false },
    }, additionalProperties: false },
    call: listRecordingTasks,
  },
  get_recording_status: {
    description: "查询机构/个人会议预约及处理状态计数和正在处理的会议预览，不取全文。",
    inputSchema: { type: "object", properties: {
      meeting_scope: { type: "string", enum: ["institution", "personal"], default: "institution" },
    }, additionalProperties: false },
    call: getRecordingStatus,
  },
  get_recording_task: {
    description: "按 list_recording_tasks 返回的 roadshow_id 查预约/录制信息；同会议多个微信群时传 wechat_group 消歧。",
    inputSchema: { type: "object", properties: {
      meeting_scope: { type: "string", enum: ["institution", "personal"], default: "institution" },
      roadshow_id: { type: "string" }, wechat_group: { type: "string" },
    }, required: ["roadshow_id"], additionalProperties: false },
    call: getRecordingTask,
  },
  auth_status: {
    description:
      "检查 AlphaEngine Desktop 是否可用；不会返回 token。status 区分 ready / app_not_running / no_module_page / token_expired，no_module_page 表示登录正常但 App 里没开模块页。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    call: authStatus,
  },
};

function rpcSuccess(id, result) {
  return { jsonrpc: "2.0", id, result };
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

async function handleRpc(message) {
  if (!message || message.jsonrpc !== "2.0") return rpcError(message?.id ?? null, -32600, "Invalid Request");
  const { id, method, params } = message;
  if (method === "notifications/initialized" || method?.startsWith("notifications/")) return null;
  if (method === "initialize") {
    return rpcSuccess(id, {
      protocolVersion: params?.protocolVersion || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "alphaengine-mcp", version: VERSION },
    });
  }
  if (method === "ping") return rpcSuccess(id, {});
  if (method === "tools/list") {
    return rpcSuccess(id, {
      tools: Object.entries(TOOLS).map(([name, tool]) => ({
        name,
        description: tool.description,
        inputSchema: tool.inputSchema,
      })),
    });
  }
  if (method === "tools/call") {
    const tool = TOOLS[params?.name];
    if (!tool) return rpcError(id, -32601, `Tool not found: ${params?.name || ""}`);
    try {
      const result = await tool.call(params?.arguments || {});
      return rpcSuccess(id, {
        content: [{ type: "text", text: JSON.stringify(result) }],
        structuredContent: result,
        isError: false,
      });
    } catch (error) {
      return rpcSuccess(id, {
        content: [{ type: "text", text: String(error.message || error) }],
        isError: true,
      });
    }
  }
  return rpcError(id, -32601, `Method not found: ${method}`);
}

function writeJson(response, status, body) {
  const text = body === null ? "" : JSON.stringify(body);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(text),
    "Cache-Control": "no-store",
  });
  response.end(text);
}

async function readJsonBody(request) {
  let body = "";
  for await (const chunk of request) {
    body += chunk;
    if (body.length > 2 * 1024 * 1024) throw new Error("Request body too large");
  }
  return JSON.parse(body || "{}");
}

function startHttp() {
  const server = http.createServer(async (request, response) => {
    try {
      if (request.method === "GET" && request.url === "/health") {
        const status = await authStatus();
        return writeJson(response, 200, {
          ok: true,
          service: "alphaengine-mcp",
          version: VERSION,
          ...status,
        });
      }
      if (request.method !== "POST" || request.url !== "/mcp") {
        return writeJson(response, 404, { error: "Not found" });
      }
      const body = await readJsonBody(request);
      if (Array.isArray(body)) {
        const results = (await Promise.all(body.map(handleRpc))).filter(Boolean);
        return writeJson(response, results.length ? 200 : 204, results.length ? results : null);
      }
      const result = await handleRpc(body);
      return writeJson(response, result ? 200 : 204, result);
    } catch (error) {
      return writeJson(response, 400, rpcError(null, -32700, String(error.message || error)));
    }
  });
  server.listen(PORT, HOST, () => {
    process.stderr.write(`[alphaengine-mcp] listening on http://${HOST}:${PORT}/mcp (v${VERSION})\n`);
  });
  const shutdown = () => server.close(() => process.exit(0));
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

function startStdio() {
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  input.on("line", async (line) => {
    if (!line.trim()) return;
    let response;
    try {
      response = await handleRpc(JSON.parse(line));
    } catch (error) {
      response = rpcError(null, -32700, String(error.message || error));
    }
    if (response) process.stdout.write(`${JSON.stringify(response)}\n`);
  });
}

function isValidDocumentId(id) { return typeof id === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,159}$/.test(id); }

// Local ingestion reuses the authenticated reader; importing must not start a server.
export { isValidDocumentId, fetchDocumentDetail, searchLibrary, listFilters, listRecordingTasks, getRecordingStatus, getRecordingTask,
  getDocumentInfo, listAttachments, selectDocumentText, documentFileCandidates, extensionFor, compactSearchResult,
  documentMetadata, resultMatches, requireDatePair, TOOLS, handleRpc };
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.includes("--stdio")) startStdio();
  else startHttp();
}
