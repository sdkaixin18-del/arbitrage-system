import { request } from "../apiClient";
import type {
  CrawlResult,
  ExchangeAnnouncementPushResult,
  ExchangeAnnouncementsResponse,
  ExchangeDelistingOpportunityDeleteResponse,
  ExchangeDelistingOpportunityMuteResponse,
  NetworkMessagesOverview
} from "../api";

export const messagingApi = {
  exchangeAnnouncements: (refresh = false) =>
    request<ExchangeAnnouncementsResponse>(
      `/api/exchange-announcements${refresh ? "?refresh=1" : ""}`
    ),
  pushExchangeAnnouncements: () =>
    request<ExchangeAnnouncementPushResult>("/api/exchange-announcements/push", {
      method: "POST"
    }),
  updateExchangeDelistingOpportunityMute: (symbol: string, muted: boolean) =>
    request<ExchangeDelistingOpportunityMuteResponse>(
      `/api/exchange-announcements/opportunities/${encodeURIComponent(symbol)}/mute?muted=${muted ? "true" : "false"}`,
      { method: "PATCH" }
    ),
  deleteExchangeDelistingOpportunityPair: (watchId: number, pairKey: string) =>
    request<ExchangeDelistingOpportunityDeleteResponse>(
      `/api/exchange-announcements/opportunities/${watchId}/pairs/${encodeURIComponent(pairKey)}`,
      { method: "DELETE" }
    ),
  networkMessages: (refresh = false) =>
    request<NetworkMessagesOverview>(
      `/api/network-messages${refresh ? "?refresh=1" : ""}`
    ),
  crawlNetworkHomeFeed: () =>
    request<CrawlResult>("/api/network-messages/crawl/home-feed", {
      method: "POST"
    }),
  crawlNetworkZsxq: () =>
    request<CrawlResult>("/api/network-messages/crawl/zsxq", {
      method: "POST"
    })
};
