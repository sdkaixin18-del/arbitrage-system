/** Asset identity uses the chain ID; history uses the provider's network key. */
export const ASTRO_CHAINS = [
  { chainId: "501", network: "solana", label: "Solana", icon: "sol.png" },
  { chainId: "56", network: "bsc", label: "BNB Smart Chain", icon: "bsc.png" },
  { chainId: "1", network: "eth", label: "Ethereum", icon: "eth.svg" },
  { chainId: "8453", network: "base", label: "Base", icon: "base.png" },
  { chainId: "42161", network: "arbitrum", label: "Arbitrum", icon: "arbitrum.svg" },
  { chainId: "4663", network: "robinhood", label: "Robinhood Chain", icon: "robinhood.webp" }
] as const;

export default function AstroChainLabel({ chain, fallback }: { chain: string; fallback?: string }) {
  const info = ASTRO_CHAINS.find(item => item.chainId === chain || item.network === chain);
  return <span className="astro-chain-label" data-chain={info?.chainId || chain}>
    {info && <img src={`/chain-logos/${info.icon}`} width={18} height={18} alt="" aria-hidden="true" />}
    <span>{info?.label || fallback || chain || "待补全"}</span>
  </span>;
}
