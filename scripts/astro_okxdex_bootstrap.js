import { startOkxdexChainNoteSync, syncOkxdexChainNotes } from "./okxdex-chain-notes.js";

await syncOkxdexChainNotes();
const bootstrapScript = document.querySelector('script[src$="okxdex-chain-note-bootstrap.js"]');
const appModule = bootstrapScript?.dataset.appModule;
if (!appModule) throw new Error("Astro 主页面模块地址未配置");
await import(appModule);
startOkxdexChainNoteSync();
