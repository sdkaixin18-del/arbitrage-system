import { request } from "../apiClient";
import type {
  CryptoExchange,
  CryptoFsBorrowSearchResponse,
  CryptoFsObservationSummary,
  CryptoFsSchedulerStatus,
  CryptoFsSignalsResponse,
  CryptoFundingCapWatchResponse,
  CryptoFundingCloudStatus,
  CryptoFundingFormationBatchResponse,
  CryptoFundingFormationResponse,
  CryptoFundingFormationWatchResponse,
  CryptoFundingPredictionReviewResponse,
  CryptoSettings,
  CryptoSymbolMapping,
  CryptoSymbolMappingPayload,
  CryptoSymbolMappingScanResponse,
  CryptoSymbolMappingUpdatePayload
} from "../api";

export const cryptoApi = {
  fsBorrowSearch: (symbol: string, refresh = false) =>
    request<CryptoFsBorrowSearchResponse>(
      `/api/fs/borrow-search?symbol=${encodeURIComponent(symbol)}${refresh ? "&refresh=true" : ""}`
    ),
  fsFundingCapWatch: () =>
    request<CryptoFundingCapWatchResponse>("/api/fs/funding-cap-watch"),
  fsFundingFormation: (
    exchange: CryptoExchange,
    symbol: string,
    targetRate?: number
  ) => {
    const params = new URLSearchParams({
      exchange,
      symbol
    });
    if (typeof targetRate === "number" && Number.isFinite(targetRate)) {
      params.set("target_rate", String(targetRate));
    }
    return request<CryptoFundingFormationResponse>(
      `/api/fs/funding-formation?${params.toString()}`
    );
  },
  fsFundingFormationBatch: (
    items: {
      id: string;
      exchange: CryptoExchange;
      symbol: string;
      targetRate?: number;
    }[]
  ) =>
    request<CryptoFundingFormationBatchResponse>("/api/fs/funding-formation/batch", {
      method: "POST",
      body: JSON.stringify({ items })
    }),
  fsFundingFormationWatch: () =>
    request<CryptoFundingFormationWatchResponse>("/api/fs/funding-formation/watch"),
  syncFsFundingFormationWatch: (
    items: {
      id: string;
      exchange: CryptoExchange;
      symbol: string;
      targetRate?: number;
    }[]
  ) =>
    request<CryptoFundingFormationWatchResponse>("/api/fs/funding-formation/watch", {
      method: "PUT",
      body: JSON.stringify({ items })
    }),
  fsFundingPredictionReview: (days = 30, checkpointMinutes = 15, modelVersion = "") =>
    request<CryptoFundingPredictionReviewResponse>(
      `/api/fs/funding-formation/review?days=${days}&checkpoint_minutes=${checkpointMinutes}&model_version=${encodeURIComponent(modelVersion)}`
  ),
  fsFundingCloudStatus: () =>
    request<CryptoFundingCloudStatus>("/api/fs/funding-formation/cloud-status"),
  addFsFundingCapWatch: (symbol: string, exchanges: CryptoExchange[]) =>
    request<CryptoFundingCapWatchResponse>(
      `/api/fs/funding-cap-watch?symbol=${encodeURIComponent(symbol)}&exchanges=${encodeURIComponent(exchanges.join(","))}`,
      { method: "POST" }
    ),
  updateFsFundingCapWatch: (symbol: string, exchanges: CryptoExchange[]) =>
    request<CryptoFundingCapWatchResponse>(
      `/api/fs/funding-cap-watch?symbol=${encodeURIComponent(symbol)}&exchanges=${encodeURIComponent(exchanges.join(","))}`,
      { method: "PATCH" }
    ),
  deleteFsFundingCapWatch: (symbol: string) =>
    request<CryptoFundingCapWatchResponse>(
      `/api/fs/funding-cap-watch?symbol=${encodeURIComponent(symbol)}`,
      { method: "DELETE" }
    ),
  refreshFsFundingCapWatch: () =>
    request<CryptoFundingCapWatchResponse>("/api/fs/funding-cap-watch/refresh", {
      method: "POST"
    }),
  fsSignals: (limit = 50) =>
    request<CryptoFsSignalsResponse>(`/api/fs/signals?limit=${limit}`),
  astroAutoCardStatus: () =>
    request<NonNullable<CryptoFsSignalsResponse["astroAutoCard"]>>(
      "/api/fs/astro-auto-card/status"
    ),
  recheckAstroSubmission: (submissionId: string) =>
    request<{result: {submissionId: string; state: string; message: string}; pendingSubmissions: NonNullable<NonNullable<CryptoFsSignalsResponse["astroAutoCard"]>["pendingSubmissions"]>}>(
      "/api/fs/astro-auto-card/submissions/recheck",
      {method:"POST",body:JSON.stringify({submissionId})}
    ),
  resolveAstroSubmissionNotExecuted: (payload: {submissionId: string; evidence: string}) =>
    request<{result: {submissionId: string; state: string}; pendingSubmissions: NonNullable<NonNullable<CryptoFsSignalsResponse["astroAutoCard"]>["pendingSubmissions"]>}>(
      "/api/fs/astro-auto-card/submissions/resolve",
      {method:"POST",body:JSON.stringify({...payload,confirmNotExecuted:true})}
    ),
  confirmAstroDexMapping: (payload: {exchange: string; symbol: string; chainIndex: string; contractAddress: string}) =>
    request<NonNullable<CryptoFsSignalsResponse["astroAutoCard"]>>("/api/fs/astro-auto-card/dex-mappings/confirm", {method:"POST",body:JSON.stringify(payload)}),
  updateAstroSpreadSubscriptions: (payload: {
    markets: string[];
    minVolumeUsdt: number;
    blockedPairs: { marketKey: string; symbol: string }[];
    blockedCoins: string[];
    dexMappedAssets?: {
      exchange?: string;
      symbol: string;
      chainIndex: string;
      contractAddress: string;
    }[];
    deleteRearmPct?: number;
    deletePullbackPctPoints?: number;
    ffMinOpenSpreadPct?: number;
    ffBybitSellExceptionEnabled?: boolean;
    sfMinOpenSpreadPct?: number;
    sfOkxdexMinOpenSpreadPct?: number;
    sfPancakeswapV3MinOpenSpreadPct?: number;
    sfMinShortFundingRatePct?: number;
    sfOkxdexAutoCardEnabled?: boolean;
    sfPancakeswapV3AutoCardEnabled?: boolean;
    fsBorrowAutoCardEnabled?: boolean;
    fsBorrowMinCycleProfitPct?: number;
    fsBorrowMinOpenSpreadPct?: number;
    confirmations?: number;
    maxQuoteAgeSeconds?: number;
    excludeDelistedExchangeCards?: boolean;
    greaterPriceAlertPct?: number | null;
    priceChangeAlertPct?: number | null;
    priceChangeAlertOnlyRise?: boolean;
    minNotionalUsdt?: number;
    maxNotionalUsdt?: number;
  }) =>
    request<NonNullable<CryptoFsSignalsResponse["astroAutoCard"]>>(
      "/api/fs/astro-auto-card/subscriptions",
      {
        method: "PUT",
        body: JSON.stringify(payload)
      }
    ),
  fsObservationSummary: (days = 7) =>
    request<CryptoFsObservationSummary>(`/api/fs/observations/summary?days=${days}`),
  fsScheduler: () => request<CryptoFsSchedulerStatus>("/api/fs/scheduler"),
  startFsScheduler: () =>
    request<CryptoFsSchedulerStatus>("/api/fs/scheduler/start", {
      method: "POST"
    }),
  pauseFsScheduler: () =>
    request<CryptoFsSchedulerStatus>("/api/fs/scheduler/pause", {
      method: "POST"
    }),
  fsSettings: () => request<CryptoSettings>("/api/fs/settings"),
  scanFsSymbolMappings: () =>
    request<CryptoSymbolMappingScanResponse>("/api/fs/symbol-mappings/scan", {
      method: "POST"
    }),
  createFsSymbolMapping: (payload: CryptoSymbolMappingPayload) =>
    request<CryptoSymbolMapping>("/api/fs/symbol-mappings", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateFsSymbolMapping: (id: number, payload: CryptoSymbolMappingUpdatePayload) =>
    request<CryptoSymbolMapping>(`/api/fs/symbol-mappings/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteFsSymbolMapping: (id: number) =>
    request<{ status: string; message: string }>(
      `/api/fs/symbol-mappings/${id}`,
      { method: "DELETE" }
    ),
};
